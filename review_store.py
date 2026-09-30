# -*- coding: utf-8 -*-
"""
검토카드 저장소 (서울형 조례정비 레이더)
========================================
법령 변경 1건이 어느 조례·조문에 걸리는지를 '변경사건'으로 저장하고,
담당부서·기한·상태·판단사유와 변경 이력을 남긴다. (기관 PC 로컬 SQLite, 외부 전송 없음)

  · 사건 키 = 법령ID | 법령 버전(법령일련번호) | 조례ID | 인용조문 | 인용명칭
    → 같은 자료를 다시 조회해도 사건이 늘지 않는다.
  · 법령 버전이 바뀌면 새 사건을 만들고 이전 사건에 연결한다(이전 결론은 그대로 보존).
  · '완료'·'정비 불필요'는 판단 사유 없이 종결할 수 없다.
"""

import csv
import os
import shutil
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 2   # 2: 현행 조문 전문(article_text)·대비표 초안(draft) 추가

ST_NEW, ST_REVIEW, ST_MORE, ST_NONEED, ST_PUSH, ST_DONE = (
    "신규", "검토 중", "추가 확인", "정비 불필요", "정비 추진", "완료")
STATUSES = [ST_NEW, ST_REVIEW, ST_MORE, ST_NONEED, ST_PUSH, ST_DONE]
ALLOWED = {
    ST_NEW: {ST_REVIEW},
    ST_REVIEW: {ST_MORE, ST_NONEED, ST_PUSH},
    ST_MORE: {ST_REVIEW, ST_NONEED, ST_PUSH},
    ST_PUSH: {ST_DONE},
    ST_NONEED: set(),
    ST_DONE: set(),
}
NEED_REASON = {ST_NONEED, ST_DONE}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY,
  event_key TEXT UNIQUE NOT NULL,
  law_name TEXT, law_id TEXT, law_mst TEXT, law_date TEXT,
  ord_name TEXT, ord_id TEXT, ord_mst TEXT, gov TEXT, kind TEXT,
  jo TEXT, cited TEXT, is_old INTEGER, snippet TEXT,
  source TEXT, fetched_at TEXT,
  status TEXT NOT NULL DEFAULT '신규', dept TEXT DEFAULT '', due TEXT DEFAULT '',
  reason TEXT DEFAULT '', reviewer TEXT DEFAULT '',
  prev_event_id INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY, event_id INTEGER, at TEXT,
  field TEXT, old TEXT, new TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS draft(
  event_id INTEGER PRIMARY KEY, kind TEXT, current_text TEXT, draft_text TEXT, memo TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS scan_log(
  id INTEGER PRIMARY KEY, at TEXT, law_name TEXT, scope TEXT, source TEXT,
  n_found INTEGER, n_new INTEGER, n_dup INTEGER, n_relinked INTEGER, n_failed INTEGER, capped INTEGER);
"""


class ReviewError(Exception):
    pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def event_key(law_id, law_mst, ord_id, jo, cited):
    return "|".join(str(x or "") for x in (law_id, law_mst, ord_id, jo, cited))


class ReviewStore:
    def __init__(self, path="radar.db"):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self):
        """이전 버전 DB(v1)에 새 칸을 더한다. 기존 자료는 그대로 둔다."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(event)")}
        if "article_text" not in cols:
            self.db.execute("ALTER TABLE event ADD COLUMN article_text TEXT DEFAULT ''")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self):
        self.db.close()

    # 등록 -------------------------------------------------------------
    def register(self, records, source, law_name="", scope="", n_failed=0, capped=False):
        """records: [{law_name, law_id, law_mst, law_date, ord_name, ord_id, ord_mst,
        gov, kind, jo, cited, is_old, snippet, dept}] → {'new','dup','relinked'}
        dept(법제처 자치법규 담당부서)가 있으면 검토카드 담당부서로 미리 채운다(서무 주임 입력 부담 감소)."""
        cnt = {"new": 0, "dup": 0, "relinked": 0}
        now = _now()
        for r in records:
            key = event_key(r.get("law_id"), r.get("law_mst"), r.get("ord_id"), r.get("jo"), r.get("cited"))
            row = self.db.execute("SELECT id, ord_mst FROM event WHERE event_key=?", (key,)).fetchone()
            if row:
                cnt["dup"] += 1
                if r.get("article_text"):   # 재조회 시 최신 조문 전문으로 갱신(대비표 ‘현행’ 기준)
                    self.db.execute("UPDATE event SET article_text=? WHERE id=?", (r["article_text"], row["id"]))
                cur_dept = self.db.execute("SELECT dept FROM event WHERE id=?", (row["id"],)).fetchone()[0]
                if r.get("dept") and not (cur_dept or "").strip():   # 담당부서가 비어 있을 때만 소관부서로 채움(담당자 입력은 유지)
                    self.db.execute("UPDATE event SET dept=? WHERE id=?", (r["dept"], row["id"]))
                    self._hist(row["id"], "dept", "", r["dept"], "소관부서 자동 입력(재조회)")
                if (r.get("ord_mst") or "") != (row["ord_mst"] or ""):   # 조례 개정본 → 이력만 남김
                    self.db.execute("UPDATE event SET ord_mst=?, updated_at=? WHERE id=?",
                                    (r.get("ord_mst"), now, row["id"]))
                    self._hist(row["id"], "ord_mst", row["ord_mst"], r.get("ord_mst"), "조례 버전 갱신")
                continue
            prev = self.db.execute(
                "SELECT id FROM event WHERE law_id=? AND ord_id=? AND jo=? AND cited=? AND law_mst<>? "
                "ORDER BY id DESC LIMIT 1",
                (r.get("law_id"), r.get("ord_id"), r.get("jo"), r.get("cited"), r.get("law_mst"))).fetchone()
            cur = self.db.execute(
                "INSERT INTO event(event_key,law_name,law_id,law_mst,law_date,ord_name,ord_id,ord_mst,gov,kind,"
                "jo,cited,is_old,snippet,source,fetched_at,status,dept,prev_event_id,created_at,updated_at,article_text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, r.get("law_name"), r.get("law_id"), r.get("law_mst"), r.get("law_date"),
                 r.get("ord_name"), r.get("ord_id"), r.get("ord_mst"), r.get("gov"), r.get("kind"),
                 r.get("jo"), r.get("cited"), 1 if r.get("is_old") else 0, r.get("snippet"),
                 source, now, ST_NEW, r.get("dept") or "", prev["id"] if prev else None, now, now,
                 r.get("article_text") or ""))
            note = f"법령 버전 변경 → 사건 #{prev['id']} 재검토" if prev else "신규 등록"
            self._hist(cur.lastrowid, "status", "", ST_NEW, note)
            cnt["relinked" if prev else "new"] += 1
        self.db.execute(
            "INSERT INTO scan_log(at,law_name,scope,source,n_found,n_new,n_dup,n_relinked,n_failed,capped) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now, law_name, scope, source, len(records), cnt["new"], cnt["dup"], cnt["relinked"],
             n_failed, 1 if capped else 0))
        self.db.commit()
        return cnt

    # 검토 --------------------------------------------------------------
    def update(self, event_id, status=None, dept=None, due=None, reason=None, reviewer=None):
        row = self.get(event_id)
        if row is None:
            raise ReviewError(f"사건 #{event_id} 없음")
        new_reason = row["reason"] if reason is None else reason
        new_reviewer = row["reviewer"] if reviewer is None else reviewer
        if status is not None and status != row["status"]:
            if status not in ALLOWED.get(row["status"], set()):
                raise ReviewError(f"'{row['status']}' → '{status}' 로 바꿀 수 없습니다.")
            if status in NEED_REASON and not (new_reason or "").strip():
                raise ReviewError(f"'{status}'(으)로 종결하려면 판단 사유가 필요합니다.")
            if status in NEED_REASON and not (new_reviewer or "").strip():
                raise ReviewError(f"'{status}'(으)로 종결하려면 확인자가 필요합니다.")
        changes = {"status": status, "dept": dept, "due": due, "reason": reason, "reviewer": reviewer}
        for f, v in changes.items():
            if v is None or v == row[f]:
                continue
            self.db.execute(f"UPDATE event SET {f}=?, updated_at=? WHERE id=?", (v, _now(), event_id))
            self._hist(event_id, f, row[f], v, "")
        self.db.commit()

    def get(self, event_id):
        return self.db.execute("SELECT * FROM event WHERE id=?", (event_id,)).fetchone()

    def get_by_key(self, key):
        return self.db.execute("SELECT * FROM event WHERE event_key=?", (key,)).fetchone()

    def events(self):
        return self.db.execute(
            "SELECT * FROM event ORDER BY is_old DESC, CASE status WHEN '완료' THEN 1 WHEN '정비 불필요' THEN 1 "
            "ELSE 0 END, (due='' OR due IS NULL), due, gov, ord_name").fetchall()

    def save_draft(self, event_id, kind, current_text, draft_text, memo):
        """신구조문대비표 초안 저장(담당자 수정본)."""
        self.db.execute("INSERT OR REPLACE INTO draft(event_id,kind,current_text,draft_text,memo,updated_at) "
                        "VALUES(?,?,?,?,?,?)", (event_id, kind, current_text, draft_text, memo, _now()))
        self._hist(event_id, "draft", "", kind, "대비표 초안 저장")
        self.db.commit()

    def get_draft(self, event_id):
        return self.db.execute("SELECT * FROM draft WHERE event_id=?", (event_id,)).fetchone()

    def history(self, event_id):
        return self.db.execute("SELECT * FROM history WHERE event_id=? ORDER BY id", (event_id,)).fetchall()

    def stats(self):
        """완료율 분모 = 전체 사건(미배정 포함). 미배정을 분모에서 빼지 않는다."""
        rows = self.db.execute("SELECT status, dept FROM event").fetchall()
        total = len(rows)
        by = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
        closed = by[ST_DONE] + by[ST_NONEED]
        return {"전체": total, **by, "미배정": sum(1 for r in rows if not (r["dept"] or "").strip()),
                "종결률": round(closed / total * 100, 1) if total else None}

    def _hist(self, event_id, field, old, new, note):
        self.db.execute("INSERT INTO history(event_id,at,field,old,new,note) VALUES(?,?,?,?,?,?)",
                        (event_id, _now(), field, "" if old is None else str(old), "" if new is None else str(new), note))

    # 내보내기 · 백업 ----------------------------------------------------
    def export_csv(self, path):
        cols = ["id", "gov", "ord_name", "kind", "jo", "cited", "is_old", "law_name", "law_mst", "status",
                "dept", "due", "reason", "reviewer", "prev_event_id", "source", "fetched_at", "snippet", "ord_id", "ord_mst"]
        heads = ["사건", "지자체", "자치법규명", "종류", "인용조문", "인용명칭", "현행명 외(⚠)", "법령", "법령버전", "상태",
                 "담당부서", "기한", "판단사유", "확인자", "이전사건", "출처", "조회일시", "인용문장", "조례ID", "조례MST"]
        rows = self.events()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(heads)
            for r in rows:
                w.writerow([csv_safe(r[c]) for c in cols])
        return len(rows)

    def backup(self, path):
        dst = sqlite3.connect(path)
        with dst:
            self.db.backup(dst)
        dst.close()

    def restore(self, path):
        """형식이 맞는 백업만 복원한다. 실패하면 현재 자료를 그대로 둔다."""
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            ver = src.execute("PRAGMA user_version").fetchone()[0]
            names = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            src.execute("SELECT id, event_key, status FROM event LIMIT 1")
        except sqlite3.Error as e:
            raise ReviewError(f"복원 파일을 읽을 수 없습니다(현재 자료 유지): {e}")
        if ver not in (1, SCHEMA_VERSION) or not {"event", "history"} <= names:
            src.close()
            raise ReviewError("복원 파일 형식이 맞지 않습니다(현재 자료 유지).")
        tmp = self.path + ".restore_tmp"
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        self.db.close()
        shutil.copyfile(self.path, self.path + ".before_restore")
        os.replace(tmp, self.path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()


def csv_safe(v):
    """엑셀에서 수식으로 실행되지 않도록 =,+,-,@ 로 시작하는 값 앞에 ' 를 붙인다."""
    if v is None:
        return ""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def records_from_rows(rows, law_name, law_info, current_name):
    """GUI 조회 결과(self.rows) → 검토카드 등록 레코드. 본문 검증된 인용만 등록한다."""
    out = []
    for it in rows:
        for c in it.get("cites") or []:
            out.append({
                "law_name": law_name, "law_id": (law_info or {}).get("law_id", ""),
                "law_mst": (law_info or {}).get("mst", ""), "law_date": (law_info or {}).get("promulgation", ""),
                "ord_name": it.get("name"), "ord_id": it.get("ord_id"), "ord_mst": it.get("mst"),
                "gov": it.get("gov"), "kind": it.get("kind"), "jo": c["jo"], "cited": c["term"],
                "is_old": c["term"] != current_name, "snippet": c["snippet"], "dept": it.get("dept", ""),
                "article_text": c.get("text", ""),
            })
    return out

# -*- coding: utf-8 -*-
"""
조례 정비 초안 도구 (유형② 인용 점검 + 신구조문대비표 초안)
==========================================================
  · 조례 조문 속 상위법 조문 인용(「법명」 제N조, 「법명」(이하 "법"이라 한다) … 법 제N조)을 찾는다.
  · 현행 법령 조문 목록과 대조해 ‘현행 조문 있음 / 삭제·이동 의심’을 판정하고, 대체 조문 후보를 제시한다.
    (후보는 참고용이다. 삭제된 조문 인용을 자동으로 지우거나 번호를 자동으로 바꾸지 않는다.)
  · 담당자가 고른 치환(옛 법령명 → 현행명, 확인된 조문번호 변경)만 반영해 ‘현행 / 개정안’ 초안을 만든다.
  · 대비표(제출용 문안)와 검토 메모(유형·사유·근거·추가 확인 사항)를 분리해 내보낸다.
네트워크·GUI 의존성 없음(순수 함수) — tests/test_radar.py 에서 시험.
"""

import difflib
import html
import re
from datetime import datetime

DRAFT_MARK = "초안 — 담당자 검토 전"

# ── 조문 인용 파싱 ─────────────────────────────────────────────────────
_NUM = r"제(\d+)조(?:의(\d+))?(?:\s*제(\d+)항)?(?:\s*제(\d+)호)?"


def _key(no, br):
    return f"{no}의{br}" if br else no


def law_article_label(a):
    """법령 조문 dict → '9조의2' 형태(조례·AI 결과의 '제9조의2'와 비교용)."""
    key = a.get("key") or a.get("no") or ""
    no, _, br = key.partition("의")
    return f"{no}조" + (f"의{br}" if br else "")


def find_law_refs(articles, law_terms):
    """조례 조문 목록에서 상위법(law_terms 중 하나)의 조문 인용을 모두 찾는다.
    「법명」 제N조 형태와, 「법명」(이하 "법"이라 한다) 약칭을 쓴 뒤의 '법 제N조' 형태를 모두 잡는다.
    반환: [{ord_jo, term, key, text, sentence}]"""
    terms = sorted({t for t in law_terms if t}, key=len, reverse=True)
    whole = "\n".join(c for _, c in articles)
    aliases = {}
    for t in terms:
        for m in re.finditer(r"「" + re.escape(t) + r"」\s*\(이하\s*[“\"']([^”\"']{1,10})[”\"']\s*(?:이)?라\s*한다\)", whole):
            aliases[m.group(1)] = t
    refs = []
    for title, content in articles:
        jm = re.match(r"\s*(제\d+조(?:의\d+)?)", content or "")
        ord_jo = jm.group(1) if jm else (title or "본문")
        pats = [(t, r"「" + re.escape(t) + r"」\s*(?:\(이하[^)]*\)\s*)?" + _NUM) for t in terms]
        pats += [(t, r"(?<![가-힣])" + re.escape(al) + r"\s+" + _NUM) for al, t in aliases.items()]
        seen = set()
        for t, pat in pats:
            for m in re.finditer(pat, content or ""):
                if m.start() in seen:
                    continue
                seen.add(m.start())
                no, br = m.group(1), m.group(2)
                s0 = content.rfind(".", 0, m.start()) + 1
                s1 = content.find(".", m.end())
                refs.append({"ord_jo": ord_jo, "term": t, "key": _key(no, br), "text": m.group(0),
                             "sentence": content[s0: s1 + 1 if s1 >= 0 else None].strip()})
    return refs


def _words(text):
    return {w for w in re.findall(r"[가-힣]{2,}", text or "") if w not in ("따라", "경우", "필요한", "사항", "이하", "한다")}


def check_refs(refs, law_articles, current_name, current_aliases=()):
    """조문 인용을 현행 법령 조문 목록과 대조한다. current_aliases: 현행 약칭(옛 법령명으로 보지 않음).
    status: '현행 조문 있음' / '현행 조문 있음(옛 법령명 — 내용 대조 필요)' / '현행 법에 없음(삭제·이동 의심)'
    candidate: 이동 이력이 있거나 제목·내용 단어가 가장 많이 겹치는 현행 조문(참고용, 확정 아님)."""
    by_key = {a.get("key") or a.get("no"): a for a in law_articles}
    out = []
    for r in refs:
        a = by_key.get(r["key"])
        old = r["term"] not in {current_name, *current_aliases}
        res = dict(r)
        res["label"] = "제" + law_article_label({"key": r["key"]})
        if a:
            res["status"] = "현행 조문 있음(옛 법령명 — 내용 대조 필요)" if old else "현행 조문 있음"
            res["current_title"] = a.get("title", "")
            res["candidate"] = None
        else:
            res["status"] = "현행 법에 없음(삭제·이동 의심) — 추가 확인 필요"
            res["current_title"] = ""
            moved = [x for x in law_articles
                     if re.sub(r"\D", "", x.get("moved_from") or "") == re.sub(r"\D", "", r["key"]) and x.get("moved_from")]
            if moved:
                c, why = moved[0], "조문 이동 이력"
            else:
                ws = _words(r["sentence"])
                scored = sorted(((len(ws & _words((x.get("title") or "") + " " + (x.get("content") or "")[:200])), x)
                                 for x in law_articles), key=lambda s: -s[0])
                c, why = (scored[0][1], f"단어 {scored[0][0]}개 일치") if scored and scored[0][0] >= 2 else (None, "")
            res["candidate"] = {"label": "제" + law_article_label(c), "title": c.get("title", ""), "why": why} if c else None
        out.append(res)
    return out


# ── 개정안 초안 ────────────────────────────────────────────────────────
def apply_replacements(text, replacements):
    """replacements: [(old, new)] — 담당자가 확인한 치환만 순서대로 적용(「」·조사 보존)."""
    for old, new in replacements:
        if old and new is not None:
            text = text.replace(old, new)
    return text


def name_replacements(current_name, cited_names):
    """옛 법령명 → 현행명 치환 목록(「」 안의 정확한 명칭만)."""
    return [(f"「{n}」", f"「{current_name}」") for n in cited_names if n and n != current_name]


def diff_segments(old, new):
    """어절 단위 차이 → ([(text, changed)], [(text, changed)]) 현행·개정안 각각(대비표 밑줄 표시용)."""
    tok = r"\s+|「[^」]*」|[^\s「]+"   # 「법령명」은 붙어 있어도 한 단위로 표시
    ta, tb = re.findall(tok, old), re.findall(tok, new)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    a, b = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            a.append(("".join(ta[i1:i2]), False)); b.append(("".join(tb[j1:j2]), False))
        else:
            if i2 > i1: a.append(("".join(ta[i1:i2]), True))
            if j2 > j1: b.append(("".join(tb[j1:j2]), True))
    return a, b


def followup_checks(old_text, new_text):
    """조문 신설·삭제·번호 변경에 따라 사람이 확인할 사항."""
    notes = []
    if old_text.strip() in ("", "<신 설>"):
        notes.append("조문 신설: 이후 조문 번호와 다른 조문의 인용(‘제N조에 따른’) 영향 확인")
    if new_text.strip() in ("", "<삭 제>"):
        notes.append("조문 삭제: 이 조문을 인용하는 다른 조문·규칙·서식 확인")
    if re.findall(r"제\d+조", old_text) != re.findall(r"제\d+조", new_text):
        notes.append("인용 조문번호 변경: 바뀐 번호가 현행 법령의 같은 내용인지 원문으로 확인")
    if re.findall(r"^\s*[①-⑳]|\n\s*[①-⑳]", old_text) != re.findall(r"^\s*[①-⑳]|\n\s*[①-⑳]", new_text):
        notes.append("항 구성 변경: 항·호 번호와 이를 인용하는 부분 확인")
    return notes


def safe_filename(gov, ord_name, jo, limit=120):
    """대비표·메모 파일 이름: 조례명에 이미 들어 있는 기관명은 한 번만, Windows에서 못 쓰는 문자는 _ 로."""
    name = ord_name if (ord_name or "").startswith(gov or "") else f"{gov}_{ord_name}"
    return re.sub(r'[\\/:*?"<>|\s_]+', "_", f"{name}_{jo}").strip("_")[:limit]


def _marked_html(segs):
    return "".join(f"<u><b>{html.escape(t)}</b></u>" if ch else html.escape(t) for t, ch in segs).replace("\n", "<br>")


def export_comparison(path, title, rows, source_note):
    """신구조문대비표 초안(HTML, 한글에서 열어 편집 가능). rows: [(현행, 개정안)] — 변경 부분 밑줄."""
    body = []
    for old, new in rows:
        a, b = diff_segments(old, new)
        body.append(f"<tr><td>{_marked_html(a)}</td><td>{_marked_html(b)}</td></tr>")
    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:'함초롬바탕','맑은 고딕',serif;font-size:11pt}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #000;padding:6px;vertical-align:top;width:50%}} th{{background:#eee}} .d{{color:#b00;font-weight:bold}}</style></head><body>
<p class="d">[{DRAFT_MARK}] 최종 개정 여부와 문안은 담당자가 판단합니다.</p>
<h3>{html.escape(title)} 신·구조문대비표(초안)</h3>
<table><tr><th>현 행</th><th>개 정 안</th></tr>{''.join(body)}</table>
<p style="font-size:9pt">{html.escape(source_note)}</p></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def build_memo(kind, ordinance, gov, dept, law_name, basis, reason, checks, source_note):
    """검토 메모(대비표와 분리): 정비 유형·개정 사유·근거·추가 확인 사항·출처."""
    lines = [f"[{DRAFT_MARK}] 검토 메모", "",
             f"○ 대상: {ordinance} ({gov}{' / ' + dept if dept else ''})",
             f"○ 정비 유형: {kind}", f"○ 근거 법령: 「{law_name}」", "○ 근거·확인 내용:"]
    lines += [f"  - {b}" for b in basis] or ["  - (없음)"]
    lines += [f"○ 개정 사유(초안): {reason}", "○ 추가 확인 사항:"]
    lines += [f"  - {c}" for c in checks] or ["  - (없음)"]
    lines += [f"○ 출처·기준일: {source_note}", f"○ 작성: {datetime.now():%Y-%m-%d %H:%M} (프로그램 초안)"]
    return "\n".join(lines)


def auto_type2_draft(article_text, current_name, cited_names, ref_results):
    """유형② 자동 초안: 옛 법령명만 현행명으로 바꾼다. 조문번호는 바꾸지 않고 확인 사항으로 남긴다."""
    new = apply_replacements(article_text, name_replacements(current_name, cited_names))
    basis, checks = [], followup_checks(article_text, new)
    for r in ref_results:
        line = f"{r['ord_jo']}의 {r['text']} → {r['status']}"
        if r.get("current_title"):
            line += f" (현행 {r['label']} {r['current_title']})"
        basis.append(line)
        if r.get("candidate"):
            c = r["candidate"]
            checks.append(f"{r['text']}: 대체 후보 {c['label']}({c['title']}, {c['why']}) — 내용이 같은지 확인 후 번호 변경 여부 결정")
        elif "내용 대조" in r["status"]:
            checks.append(f"{r['text']}: 옛 법령명 기준 조문번호 — 현행 {r['label']}({r.get('current_title', '')})와 내용이 같은지 원문 대조")
        elif "없음" in r["status"]:
            checks.append(f"{r['text']}: 대체 근거 불명확 — 다른 법률로 이관 여부 등 추가 확인 필요(자동 삭제하지 않음)")
    return new, basis, checks


# ── 유형④ 용어·표현 정비 ────────────────────────────────────────────────
# 법령 개정으로 바뀐 용어가 조례 본문에 남아 있는지 찾는다(「」 안의 법령명은 유형②가 담당하므로 제외).
# 같은 문장이 권리·의무(지원·감면·부과 등)와 관련되면 단순 용어 정리가 아니므로 ‘별도 검토’로 표시한다.
RIGHTS_RE = re.compile(r"지원|감면|면제|부과|징수|과태료|벌칙|자격|의무|금지|허가|인가|신고|보조|수수료|사용료|요금|지급|제한")


def load_terms(path):
    """용어 목록 파일: 한 줄에 ‘옛 용어 => 새 용어 | 근거·비고 | 제외: 단어1, 단어2’ (# 주석).
    ‘제외’에 적은 단어의 일부로 나온 경우(예: 문화재 ← 서울문화재단)는 찾지 않는다."""
    terms = []
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=>" not in ln:
                continue
            old, rest = ln.split("=>", 1)
            segs = [s.strip() for s in rest.split("|")]
            new, note = segs[0], (segs[1] if len(segs) > 1 else "")
            excl = [w.strip() for s in segs[2:] if s.startswith("제외") for w in s.split(":", 1)[-1].split(",") if w.strip()]
            if old.strip() and new:
                terms.append((old.strip(), new, note, excl))
    return terms


def find_term_issues(articles, terms):
    """조문별 옛 용어 사용 위치 → [{ord_jo, old, new, note, sentence, rights}] (「」 안 제외)."""
    out = []
    for title, content in articles:
        jm = re.match(r"\s*(제\d+조(?:의\d+)?)", content or "")
        ord_jo = jm.group(1) if jm else (title or "본문")
        masked = re.sub(r"「[^」]*」", lambda m: " " * len(m.group(0)), content or "")
        for term in terms:
            old, new, note = term[:3]
            excl = term[3] if len(term) > 3 else []
            for m in re.finditer(re.escape(old), masked):
                if any(e.start() <= m.start() and m.end() <= e.end()
                       for x in excl for e in re.finditer(re.escape(x), masked)):
                    continue   # 제외 단어의 일부(예: 서울문화재단의 ‘문화재’)
                s0 = content.rfind(".", 0, m.start()) + 1
                s1 = content.find(".", m.end())
                sent = content[s0: s1 + 1 if s1 >= 0 else None].strip()
                out.append({"ord_jo": ord_jo, "old": old, "new": new, "note": note, "sentence": sent,
                            "rights": bool(RIGHTS_RE.search(sent)), "pos": m.start()})
                break   # 조문마다 용어별 첫 위치만(치환은 담당자가 선택한 범위에서)
    return out


def replace_term_outside_brackets(text, old, new):
    """「」 안(법령명)은 건드리지 않고 본문 용어만 바꾼다."""
    parts = re.split(r"(「[^」]*」)", text)
    return "".join(p if p.startswith("「") else p.replace(old, new) for p in parts)

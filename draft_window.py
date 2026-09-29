# -*- coding: utf-8 -*-
"""신구조문대비표 초안 창 — 현행 조문·정비 근거 확인 → 개정안 편집 → 변경 부분 확인 → 저장·내보내기."""

import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import revision as R


class DraftWindow(tk.Toplevel):
    """preset(선택): 유형①·③ AI 결과를 채택했을 때 {kind, current_text, draft_text, basis, checks, reason}"""

    def __init__(self, master, store, event, api_factory=None, preset=None):
        super().__init__(master)
        self.store, self.ev, self.api_factory = store, event, api_factory
        self.refs = []
        self.title(f"📝 신구조문대비표 초안 — {event['ord_name']} {event['jo']}  [{R.DRAFT_MARK}]")
        self.geometry("1180x780")
        saved = store.get_draft(event["id"])
        cur = (preset or {}).get("current_text") or event["article_text"] or event["snippet"] or ""
        if preset:
            self.kind = preset.get("kind", "")
            new, basis, checks, reason = preset.get("draft_text", cur), preset.get("basis", []), preset.get("checks", []), preset.get("reason", "")
        else:
            self.kind = "유형② 변경된 법령명·조문 인용 정비"
            new, basis, checks = R.auto_type2_draft(cur, event["law_name"], [event["cited"]], [])
            reason = f"상위법 명칭 변경(「{event['cited']}」 → 「{event['law_name']}」)에 따른 인용 정비" if event["is_old"] else "상위법 조문 인용 정비"
        if saved and not preset:
            self.kind, cur, new = saved["kind"], saved["current_text"], saved["draft_text"]
            memo = saved["memo"]
        else:
            memo = self._memo(cur, basis, checks, reason, new)
        self._build(cur, new, memo)
        if not event["article_text"] and not preset:
            self.var_note.set("⚠ 조문 전문이 저장되어 있지 않아 인용 문장만 표시합니다. 다시 조회·등록하면 전문이 채워집니다.")
        if api_factory and not preset:
            threading.Thread(target=self._load_refs, args=(cur,), daemon=True).start()

    def _memo(self, cur, basis, checks, reason, new):
        e = self.ev
        src = f"법제처 국가법령정보 공동활용 조회 {e['fetched_at']} · 조례 {e['ord_name']}(MST {e['ord_mst']}) · 법령 버전 {e['law_mst']}"
        checks = list(dict.fromkeys(checks + R.followup_checks(cur, new)))   # 중복 제거, 순서 유지
        return R.build_memo(self.kind, e["ord_name"], e["gov"], e["dept"], e["law_name"], basis, reason, checks, src)

    def _build(self, cur, new, memo):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 2))
        e = self.ev
        ttk.Label(top, text=f"{e['gov']} {e['ord_name']} {e['jo']}   ←  「{e['cited']}」   (소관부서: {e['dept'] or '미입력'})",
                  font=("Malgun Gothic", 10, "bold")).pack(anchor="w")
        ttk.Label(top, text=f"정비 유형: {self.kind}", foreground="#1565c0").pack(anchor="w")
        self.var_note = tk.StringVar(value="조문 인용 점검 중…" if self.api_factory else "")
        ttk.Label(top, textvariable=self.var_note, foreground="#8a3b00").pack(anchor="w")

        rf = ttk.LabelFrame(self, text="정비 근거 — 조문 인용 점검(삭제·이동 여부, 대체 후보는 참고용)")
        rf.pack(fill="x", padx=10, pady=4)
        self.tree = ttk.Treeview(rf, columns=("ref", "status", "cand"), show="headings", height=4)
        for c, h, w in (("ref", "조례 속 인용", 280), ("status", "판정", 360), ("cand", "대체 후보(확인 필요)", 420)):
            self.tree.heading(c, text=h); self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="x", expand=True)
        ttk.Button(rf, text="선택 후보 번호로\n개정안 바꾸기", command=self.apply_candidate).pack(side="left", padx=6)

        pan = ttk.Panedwindow(self, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=10, pady=4)
        lf, rf2 = ttk.LabelFrame(pan, text="현 행 (원문 보존)"), ttk.LabelFrame(pan, text="개 정 안 (직접 수정)")
        pan.add(lf, weight=1); pan.add(rf2, weight=1)
        self.txt_cur = tk.Text(lf, wrap="word", font=("Malgun Gothic", 10), height=14)
        self.txt_new = tk.Text(rf2, wrap="word", font=("Malgun Gothic", 10), height=14, undo=True)
        for t in (self.txt_cur, self.txt_new):
            t.pack(fill="both", expand=True)
            t.tag_configure("chg", underline=True, foreground="#b71c1c")
        self.txt_cur.insert("1.0", cur); self.txt_cur.configure(state="disabled")
        self.txt_new.insert("1.0", new)

        mf = ttk.LabelFrame(self, text="검토 메모 (대비표와 별도 파일로 저장)")
        mf.pack(fill="x", padx=10, pady=4)
        self.txt_memo = tk.Text(mf, wrap="word", font=("Malgun Gothic", 9), height=8)
        self.txt_memo.pack(fill="x")
        self.txt_memo.insert("1.0", memo)

        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Label(bf, text=f"⚠ {R.DRAFT_MARK} · 최종 개정 여부와 문안은 담당자가 판단합니다.", foreground="#8a3b00").pack(side="left")
        ttk.Button(bf, text="내보내기(대비표 + 메모)", command=self.export).pack(side="right")
        ttk.Button(bf, text="저장", command=self.save).pack(side="right", padx=6)
        ttk.Button(bf, text="변경 부분 보기", command=self.show_diff).pack(side="right")
        self.show_diff()

    # 조문 인용 점검 (API로 현행 법령 조문 목록 조회) ------------------------
    def _load_refs(self, cur):
        try:
            api = self.api_factory()
            law_arts = api.get_law_articles(self.ev["law_mst"])
            refs = R.check_refs(R.find_law_refs([("", cur)], [self.ev["law_name"], self.ev["cited"]]), law_arts, self.ev["law_name"])
            self.after(0, lambda: self._show_refs(refs))
        except Exception as ex:
            msg = str(ex).splitlines()[0]
            self.after(0, lambda: self.var_note.set("조문 인용 점검 못 함(법령 조회 실패): " + msg))

    def _show_refs(self, refs):
        self.refs = refs
        for r in refs:
            c = r.get("candidate")
            self.tree.insert("", "end", values=(r["text"], r["status"] + (f" — 현행 {r['label']} {r['current_title']}" if r["current_title"] else ""),
                                                f"{c['label']} {c['title']} ({c['why']})" if c else "-"))
        self.var_note.set(f"조문 인용 {len(refs)}건 점검 완료" if refs else "이 조문에는 상위법 조문번호 인용이 없습니다.")
        basis, checks = R.auto_type2_draft("", self.ev["law_name"], [], refs)[1:]
        if basis:
            self.txt_memo.insert("end", "\n\n[조문 인용 점검]\n" + "\n".join("  - " + b for b in basis)
                                 + ("\n[추가 확인]\n" + "\n".join("  - " + c for c in checks) if checks else ""))

    def apply_candidate(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.refs[self.tree.index(sel[0])]
        c = r.get("candidate")
        if not c:
            messagebox.showinfo("대체 후보 없음", "대체 근거가 불명확합니다. 추가 확인 후 직접 수정하세요.", parent=self)
            return
        new_ref = r["text"].replace(r["label"], c["label"], 1).replace(f"「{self.ev['cited']}」", f"「{self.ev['law_name']}」")
        txt = self.txt_new.get("1.0", "end-1c")
        cur_ref = r["text"].replace(f"「{self.ev['cited']}」", f"「{self.ev['law_name']}」")
        if cur_ref not in txt and r["text"] not in txt:
            messagebox.showinfo("바꿀 곳 없음", "개정안에서 해당 인용을 찾지 못했습니다. 직접 수정하세요.", parent=self)
            return
        txt = txt.replace(cur_ref, new_ref).replace(r["text"], new_ref)
        self.txt_new.delete("1.0", "end"); self.txt_new.insert("1.0", txt)
        self.txt_memo.insert("end", f"\n  - [담당자 선택] {r['text']} → {c['label']}({c['title']}) 로 변경(근거: {c['why']}, 원문 대조 필요)")
        self.show_diff()

    def show_diff(self):
        old, new = self.txt_cur.get("1.0", "end-1c"), self.txt_new.get("1.0", "end-1c")
        a, b = R.diff_segments(old, new)
        for t, segs in ((self.txt_cur, a), (self.txt_new, b)):
            t.tag_remove("chg", "1.0", "end")
            pos = 0
            for text, ch in segs:
                if ch:
                    t.tag_add("chg", f"1.0+{pos}c", f"1.0+{pos + len(text)}c")
                pos += len(text)

    def _values(self):
        return (self.txt_cur.get("1.0", "end-1c"), self.txt_new.get("1.0", "end-1c"), self.txt_memo.get("1.0", "end-1c"))

    def save(self):
        cur, new, memo = self._values()
        self.store.save_draft(self.ev["id"], self.kind, cur, new, memo)
        messagebox.showinfo("저장", "초안을 저장했습니다(검토카드 이력에 기록).", parent=self)

    def export(self):
        cur, new, memo = self._values()
        d = filedialog.askdirectory(parent=self, title="대비표·메모를 저장할 폴더")
        if not d:
            return
        base = R.safe_filename(self.ev["gov"], self.ev["ord_name"], self.ev["jo"])
        src = f"현행 조문 출처: 법제처 국가법령정보 공동활용(조회 {self.ev['fetched_at']})"
        p1 = R.export_comparison(os.path.join(d, f"신구조문대비표_초안_{base}.html"), self.ev["ord_name"], [(cur, new)], src)
        p2 = os.path.join(d, f"검토메모_{base}.txt")
        with open(p2, "w", encoding="utf-8") as f:
            f.write(memo)
        self.store.save_draft(self.ev["id"], self.kind, cur, new, memo)
        messagebox.showinfo("내보내기", f"저장했습니다. 대비표는 한글에서 열어 편집할 수 있습니다.\n{p1}\n{p2}", parent=self)

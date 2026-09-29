# -*- coding: utf-8 -*-
"""AI 검토 창 — 유형① 의무·위임 반영 여부 / 유형③ 취지·현실 적합성(시범). 담당자가 채택한 항목만 대비표 초안으로."""

import os
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import ai_review as A
import revision as R
from draft_window import DraftWindow
from review_store import event_key

MODES = {"1": "유형① 의무·위임 사항 반영 여부", "3": "유형③ 법령 취지·현실 적합성(시범)"}


class AIReviewWindow(tk.Toplevel):
    """ctx: {api, api_key, store, row(조회 결과 1행), law_name, law_mst, law_id, law_enforce, api_factory}"""

    def __init__(self, master, ctx):
        super().__init__(master)
        self.ctx, self.items, self.materials, self.ord_articles = ctx, [], [], []
        self.title(f"🧩 AI 검토 — {ctx['row']['name']}")
        self.geometry("1180x720")
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)
        self.var_mode = tk.StringVar(value="1")
        for k, v in MODES.items():
            ttk.Radiobutton(top, text=v, value=k, variable=self.var_mode).pack(side="left", padx=(0, 12))
        ttk.Button(top, text="정책·현장 자료 추가(.txt)", command=self.add_material).pack(side="left", padx=6)
        self.var_mat = tk.StringVar(value="자료 0건")
        ttk.Label(top, textvariable=self.var_mat).pack(side="left")
        self.btn_run = ttk.Button(top, text="검토 실행", command=self.run)
        self.btn_run.pack(side="right")
        self.var_note = tk.StringVar(value="AI 결과는 검토 후보입니다. 법적 오류나 개정 의무로 확정하지 않습니다.")
        ttk.Label(self, textvariable=self.var_note, foreground="#8a3b00").pack(anchor="w", padx=12)
        self.tree = ttk.Treeview(self, columns=("a", "b", "c", "d", "e"), show="headings", height=12)
        self.tree.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.txt = tk.Text(self, height=9, wrap="word", font=("Malgun Gothic", 9))
        self.txt.pack(fill="x", padx=10)
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=8)
        ttk.Button(bf, text="채택 → 📝 대비표 초안", command=self.adopt).pack(side="right")

    def add_material(self):
        ps = filedialog.askopenfilenames(parent=self, filetypes=[("텍스트", "*.txt *.md"), ("모든 파일", "*.*")])
        for p in ps:
            try:
                with open(p, encoding="utf-8") as f:
                    self.materials.append((os.path.basename(p), f.read()))
            except UnicodeDecodeError:
                with open(p, encoding="cp949", errors="replace") as f:
                    self.materials.append((os.path.basename(p), f.read()))
        self.var_mat.set(f"자료 {len(self.materials)}건")

    def run(self):
        self.btn_run.config(state="disabled")
        self.var_note.set("검토 중… (법 조문 조회 → 규칙으로 후보 조항 선별 → AI 대조)")
        mode = self.var_mode.get()
        threading.Thread(target=self._work, args=(mode,), daemon=True).start()

    def _work(self, mode):
        c = self.ctx
        try:
            law_arts = c["api"].get_law_articles(c["law_mst"])
            _, self.ord_articles = c["api"].get_ordinance_articles(c["row"]["mst"])
            if mode == "1":
                cand = A.obligation_articles(law_arts)
                if not cand:
                    self.after(0, lambda: self._done([], mode, "이 법에서 조례 위임 또는 지자체 의무 조항을 찾지 못했습니다."))
                    return
                items = A.review_type1(c["api_key"], c["law_name"], c.get("law_enforce"), cand, c["row"]["name"], c["row"]["gov"], self.ord_articles)
            else:
                items = A.review_type3(c["api_key"], c["law_name"], law_arts, c["row"]["name"], c["row"]["gov"], self.ord_articles, self.materials)
            known = {m.group(1)[1:] for _, t in self.ord_articles for m in [re.match(r"\s*(제\d+조(?:의\d+)?)", t)] if m}
            known |= {R.law_article_label(a) for a in law_arts}
            for it in items:   # 근거 칸의 없는 번호 = 확인 필요 / 초안 문안의 새 번호 = 신설 번호·위치 확인
                body = " ".join(str(v) for k, v in it.items() if k != "draft")
                it["_unknown"] = [u for u in dict.fromkeys(re.findall(r"제(\d+조(?:의\d+)?)", body)) if u not in known]
                it["_new_no"] = [u for u in dict.fromkeys(re.findall(r"제(\d+조(?:의\d+)?)", str(it.get("draft", "")))) if u not in known]
            note = "" if mode == "1" or self.materials else "제공 자료가 없어 현실 적합성 판단 근거가 부족합니다(법 취지 중심 검토)."
            self.after(0, lambda: self._done(items, mode, note))
        except Exception as e:
            msg = str(e).splitlines()[0]
            self.after(0, lambda: self._done([], mode, "검토 실패: " + msg))

    def _done(self, items, mode, note):
        self.btn_run.config(state="normal")
        self.items, self.mode = items, mode
        self.tree.delete(*self.tree.get_children())
        heads = (("law_article", "법 조문", 80), ("kind", "구분", 50), ("subject", "주체/대상", 190), ("judgment", "판단", 90), ("reason", "이유", 640)) \
            if mode == "1" else (("ord_article", "조례 조문", 90), ("reason", "보완 검토 이유", 420), ("evidence", "근거", 300), ("data_gap", "자료 부족", 160), ("judge", "담당자 판단", 200))
        for col, (k, h, w) in zip(("a", "b", "c", "d", "e"), heads):
            self.tree.heading(col, text=h); self.tree.column(col, width=w, anchor="w")
        for it in items:
            vals = [str(it.get(k, "")) for k, _, _ in heads]
            if mode == "1":
                vals[2] = f"{it.get('subject', '')} / {it.get('target', '')}"
            if it["_unknown"]:
                vals[-1 if mode == "1" else 1] = "⚠ 원문에 없는 조문번호 " + ",".join(it["_unknown"]) + " — " + vals[-1 if mode == "1" else 1]
            self.tree.insert("", "end", values=vals)
        self.var_note.set((f"{len(items)}건 — " if items else "") + (note or "AI 결과는 검토 후보입니다. 채택한 항목만 대비표 초안으로 넘어갑니다."))

    def on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[self.tree.index(sel[0])]
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", "\n".join(f"{k}: {v}" for k, v in it.items() if not k.startswith("_")))

    def adopt(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("채택", "표에서 항목을 선택하세요.", parent=self)
            return
        it, c, row = self.items[self.tree.index(sel[0])], self.ctx, self.ctx["row"]
        target = (it.get("ord_article") or "").strip()
        m = re.match(r"제\d+조(?:의\d+)?", target)
        cur = next((t for _, t in self.ord_articles if m and re.match(r"\s*" + re.escape(m.group(0)) + r"(?!의)", t)), None)
        if self.mode == "1":
            kind = "유형① 의무·위임 사항 반영"
            jo = m.group(0) if cur else f"신설({it.get('law_article', '')})"
            basis = [f"「{c['law_name']}」 {it.get('law_article', '')} — {it.get('kind', '')}, 주체 {it.get('subject', '-')}, "
                     f"대상 {it.get('target', '-')}, 법 시행일 {c.get('law_enforce') or '미확인'}",
                     f"AI 판단: {it.get('judgment', '')} — {it.get('reason', '')}"]
            checks = [it.get("check", ""), "법률 직접 적용 사항인지, 조례 위임 사항인지, 다른 조례에 이미 반영됐는지 확인"]
            reason = f"상위법 {it.get('law_article', '')} {it.get('kind', '')} 사항의 조례 반영 검토"
        else:
            kind = "유형③ 법령 취지·현실 적합성 보완(시범)"
            jo = m.group(0) if cur else "신설(보완 검토)"
            basis = [f"근거: {it.get('evidence', '')}"]
            checks = [it.get("judge", ""), it.get("data_gap", "")]
            reason = it.get("reason", "")
        if it.get("_unknown"):
            checks.append("AI 결과에 원문에 없는 조문번호(" + ",".join(it["_unknown"]) + ") — 확인 필요")
        if it.get("_new_no"):
            checks.append("초안 문안의 새 조문번호(" + ",".join(it["_new_no"]) + ") — 신설 번호·위치와 이후 조문 번호 확인")
        cur_text = cur or "<신 설>"
        draft = it.get("draft") or cur_text
        rec = {"law_name": c["law_name"], "law_id": c.get("law_id", ""), "law_mst": c["law_mst"], "ord_name": row["name"],
               "ord_id": row.get("ord_id"), "ord_mst": row["mst"], "gov": row["gov"], "kind": row.get("kind"), "jo": jo,
               "cited": c["law_name"], "is_old": False, "snippet": reason[:120], "dept": row.get("dept", ""),
               "article_text": cur or ""}
        c["store"].register([rec], "AI검토" + self.mode, law_name=c["law_name"])
        key = event_key(rec["law_id"], rec["law_mst"], rec["ord_id"], rec["jo"], rec["cited"])
        ev = c["store"].get_by_key(key)
        DraftWindow(self, c["store"], ev, None, preset={"kind": kind, "current_text": cur_text, "draft_text": draft,
                                                        "basis": basis, "checks": [x for x in checks if x], "reason": reason})

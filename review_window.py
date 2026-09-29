# -*- coding: utf-8 -*-
"""검토카드 창 — 변경사건 목록 · 배정 · 상태 · 사유 · 이력 (tkinter)."""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from review_store import STATUSES, ALLOWED, ReviewError


FIELD_KO = {"status": "상태", "dept": "담당부서", "due": "기한", "reason": "판단 사유",
            "reviewer": "확인자", "ord_mst": "조례 버전"}


class ReviewWindow(tk.Toplevel):
    COLS = ("id", "gov", "ord_name", "jo", "cited", "status", "dept", "due", "source")
    HEADS = {"id": "사건", "gov": "지자체", "ord_name": "자치법규명", "jo": "인용조문", "cited": "인용명칭",
             "status": "상태", "dept": "담당부서", "due": "기한", "source": "출처"}
    WIDTHS = {"id": 46, "gov": 120, "ord_name": 250, "jo": 70, "cited": 170, "status": 76,
              "dept": 100, "due": 84, "source": 60}

    def __init__(self, master, store, current_name=""):
        super().__init__(master)
        self.store = store
        self.current_name = current_name
        self.title("📋 검토카드 — 찾은 인용마다 1장: 담당부서·기한·판단 사유·이력")
        self.geometry("1180x700")
        self._build()
        self.refresh()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.var_stat = tk.StringVar()
        ttk.Label(top, textvariable=self.var_stat, foreground="#1565c0").pack(side="left")
        ttk.Button(top, text="CSV 내보내기", command=self.export).pack(side="right")
        ttk.Button(top, text="백업", command=self.backup).pack(side="right", padx=4)

        pan = ttk.Panedwindow(self, orient="vertical")
        pan.pack(fill="both", expand=True, padx=10, pady=4)
        lf = ttk.Frame(pan)
        pan.add(lf, weight=3)
        self.tree = ttk.Treeview(lf, columns=self.COLS, show="headings", selectmode="browse")
        for c in self.COLS:
            self.tree.heading(c, text=self.HEADS[c])
            self.tree.column(c, width=self.WIDTHS[c], anchor="w")
        self.tree.tag_configure("old", foreground="#c62828")
        self.tree.tag_configure("closed", foreground="#9e9e9e")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        card = ttk.LabelFrame(pan, text="검토카드")
        pan.add(card, weight=2)
        self.var_head = tk.StringVar()
        ttk.Label(card, textvariable=self.var_head, font=("Malgun Gothic", 10, "bold")).grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(6, 2))
        self.var_snip = tk.StringVar()
        ttk.Label(card, textvariable=self.var_snip, wraplength=1100, foreground="#444").grid(
            row=1, column=0, columnspan=6, sticky="w", padx=8)
        ttk.Label(card, text="상태").grid(row=2, column=0, sticky="e", padx=(8, 4), pady=6)
        self.var_status = tk.StringVar()
        self.cmb_status = ttk.Combobox(card, textvariable=self.var_status, width=12, state="readonly")
        self.cmb_status.grid(row=2, column=1, sticky="w")
        ttk.Label(card, text="담당부서").grid(row=2, column=2, sticky="e", padx=(16, 4))
        self.var_dept = tk.StringVar()
        ttk.Entry(card, textvariable=self.var_dept, width=18).grid(row=2, column=3, sticky="w")
        ttk.Label(card, text="기한(YYYY-MM-DD)").grid(row=2, column=4, sticky="e", padx=(16, 4))
        self.var_due = tk.StringVar()
        ttk.Entry(card, textvariable=self.var_due, width=12).grid(row=2, column=5, sticky="w")
        ttk.Label(card, text="판단 사유").grid(row=3, column=0, sticky="ne", padx=(8, 4))
        self.txt_reason = tk.Text(card, height=3, width=90, font=("Malgun Gothic", 9))
        self.txt_reason.grid(row=3, column=1, columnspan=5, sticky="we", pady=2)
        ttk.Label(card, text="확인자").grid(row=4, column=0, sticky="e", padx=(8, 4))
        self.var_reviewer = tk.StringVar()
        ttk.Entry(card, textvariable=self.var_reviewer, width=18).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Button(card, text="저장", command=self.save).grid(row=4, column=5, sticky="e", pady=4)
        self.lst_hist = tk.Listbox(card, height=5, font=("Malgun Gothic", 9))
        self.lst_hist.grid(row=5, column=0, columnspan=6, sticky="we", padx=8, pady=(2, 8))
        card.columnconfigure(5, weight=1)
        self.sel_id = None

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.store.events():
            tags = []
            if r["status"] in ("완료", "정비 불필요"):
                tags.append("closed")
            elif r["is_old"]:
                tags.append("old")
            cited = ("⚠ " if r["is_old"] else "") + (r["cited"] or "")
            self.tree.insert("", "end", iid=str(r["id"]), tags=tags, values=(
                r["id"], r["gov"], r["ord_name"], r["jo"], cited, r["status"], r["dept"], r["due"], r["source"]))
        s = self.store.stats()
        rate = "-" if s["종결률"] is None else f"{s['종결률']}%"
        self.var_stat.set(
            f"전체 {s['전체']}건 · 신규 {s['신규']} · 검토 중 {s['검토 중']} · 추가 확인 {s['추가 확인']} · "
            f"정비 추진 {s['정비 추진']} · 완료 {s['완료']} · 정비 불필요 {s['정비 불필요']} · "
            f"미배정 {s['미배정']}  |  종결률 {rate} (분모: 미배정 포함 전체)")

    def on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.sel_id = int(sel[0])
        r = self.store.get(self.sel_id)
        prev = f"  · 이전 사건 #{r['prev_event_id']} 재검토" if r["prev_event_id"] else ""
        self.var_head.set(f"#{r['id']}  {r['gov']} {r['ord_name']} {r['jo']}  ←  「{r['cited']}」"
                          f"  (법령: {r['law_name']} / 버전 {r['law_mst']}){prev}")
        self.var_snip.set(f"인용 원문: {r['snippet']}   [출처 {r['source']} · 조회 {r['fetched_at']}]")
        self.cmb_status.config(values=[r["status"]] + sorted(ALLOWED.get(r["status"], set()), key=STATUSES.index))
        self.var_status.set(r["status"])
        self.var_dept.set(r["dept"] or "")
        self.var_due.set(r["due"] or "")
        self.var_reviewer.set(r["reviewer"] or "")
        self.txt_reason.delete("1.0", "end")
        self.txt_reason.insert("1.0", r["reason"] or "")
        self.lst_hist.delete(0, "end")
        for h in self.store.history(self.sel_id):
            note = f"  ({h['note']})" if h["note"] else ""
            field = FIELD_KO.get(h["field"], h["field"])
            self.lst_hist.insert("end", f"{h['at']}  {field}: '{h['old']}' → '{h['new']}'{note}")

    def save(self):
        if self.sel_id is None:
            return
        try:
            self.store.update(self.sel_id, status=self.var_status.get(), dept=self.var_dept.get().strip(),
                              due=self.var_due.get().strip(), reason=self.txt_reason.get("1.0", "end").strip(),
                              reviewer=self.var_reviewer.get().strip())
        except ReviewError as e:
            messagebox.showwarning("저장 불가", str(e), parent=self)
            return
        sid = self.sel_id
        self.refresh()
        self.tree.selection_set(str(sid))
        self.tree.see(str(sid))

    def export(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile="검토카드.csv",
                                         filetypes=[("CSV 파일", "*.csv")])
        if p:
            n = self.store.export_csv(p)
            messagebox.showinfo("내보내기", f"{n}건을 저장했습니다.\n{p}", parent=self)

    def backup(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".db", initialfile="radar_backup.db",
                                         filetypes=[("DB 백업", "*.db")])
        if p:
            self.store.backup(p)
            messagebox.showinfo("백업", f"백업했습니다.\n{p}", parent=self)

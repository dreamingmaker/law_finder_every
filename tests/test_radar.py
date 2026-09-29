# -*- coding: utf-8 -*-
"""품질시험 — 네트워크·인증키 없이 합성 입력으로 실행 (python -m unittest discover tests)."""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import law_ordinance_network as lon  # noqa: E402
import radar_scan  # noqa: E402
from review_store import ReviewStore, ReviewError  # noqa: E402

CUR, OLD = "지능정보화 기본법", "국가정보화 기본법"
TERMS = [CUR, OLD]


def rec(ord_id="O1", law_mst="M1", jo="제3조", cited=OLD, law_id="L1", ord_mst="S1"):
    return {"law_name": CUR, "law_id": law_id, "law_mst": law_mst, "ord_name": "정보화 조례",
            "ord_id": ord_id, "ord_mst": ord_mst, "gov": "서울특별시 중구", "kind": "조례",
            "jo": jo, "cited": cited, "is_old": cited == OLD, "snippet": "…"}


class FakeAPI:
    def __init__(self, fail_msts=(), total=None):
        self.fail, self.total = set(fail_msts), total

    def search_ordinance_all(self, term, org=None, search=2, max_count=200, progress=None):
        items = [{"mst": "S1", "ord_id": "O1", "name": "정보화 조례", "gov": "서울특별시 중구",
                  "kind": "조례", "enforce": "20250101"},
                 {"mst": "S2", "ord_id": "O2", "name": "정보화 조례", "gov": "서울특별시 중랑구",
                  "kind": "조례", "enforce": "20250101"}]
        return (self.total or len(items)), items

    def get_ordinance_articles(self, mst):
        if mst in self.fail:
            raise lon.LawApiError("네트워크 오류")
        return {}, [("목적", f"제1조(목적) 이 조례는 「{CUR}」에 따른다."),
                    ("지원", f"제3조(지원) 「{OLD}」 제15조에 따른다.")]


LAW = {"name": CUR, "law_id": "L1", "mst": "M1", "promulgation": "20240101", "prev_name": OLD, "alias": ""}


class CitationTests(unittest.TestCase):
    def test_mixed_names_keep_both(self):
        arts = [("목적", f"제1조(목적) 「{CUR}」에 따른다."), ("지원", f"제3조(지원) 「{OLD}」에 따른다.")]
        cites = lon.find_citations(arts, TERMS)
        self.assertEqual({(c["jo"], c["term"]) for c in cites}, {("제1조", CUR), ("제3조", OLD)})
        strength, jo, _, cited = lon.analyze_link(arts, TERMS)
        self.assertEqual((strength, jo, cited), (lon.LINK_GROUND, "제3조", OLD))  # 옛 명칭 누락 없음

    def test_article_1_2_is_not_purpose(self):
        arts = [("지원", f"제1조의2(지원) 「{CUR}」에 따른다.")]
        strength, jo, _, _ = lon.analyze_link(arts, TERMS)
        self.assertEqual((strength, jo), (lon.LINK_CITE, "제1조의2"))

    def test_purpose_title_without_citation(self):
        arts = [("목적", "제1조(목적) 주민 편익을 증진한다.")]
        self.assertEqual(lon.analyze_link(arts, TERMS)[0], lon.LINK_WEAK)
        self.assertEqual(lon.find_citations(arts, TERMS), [])

    def test_overlapping_terms_not_double_counted(self):
        arts = [("지원", f"제3조(지원) 「{OLD}」에 따른다.")]
        cites = lon.find_citations(arts, [CUR, OLD, "정보화 기본법"])
        self.assertEqual([c["term"] for c in cites], [OLD])

    def test_ai_unknown_article_flagged(self):
        text = "| 제3조 | ... | 제99조의2로 이동 |"
        self.assertEqual(lon.unverified_article_refs(text, {"1조", "3조"}), ["99조의2"])


    def test_obligation_articles_for_local_government_only(self):
        arts = [{"no": "14", "title": "공공지능정보화", "content": "제14조 국가기관등은 … 공공지능정보화를 추진하여야 한다."},
                {"no": "15", "title": "지역지능정보화", "content": "제15조 지방자치단체는 … 추진할 수 있다."},
                {"no": "6", "title": "기본계획", "content": "제6조 정부는 기본계획을 수립하여야 한다."}]
        self.assertEqual([a["no"] for a in lon.obligation_articles(arts)], ["14"])


class ScanTests(unittest.TestCase):
    def test_fetch_failure_is_unverified_not_empty(self):
        rec_, fail, s, _refs = radar_scan.scan_law(FakeAPI(fail_msts={"S2"}), LAW, "6110000", log=lambda *_: None)
        self.assertEqual(s["미검증"], 1)
        self.assertEqual(fail[0]["mst"], "S2")
        self.assertEqual(s["이전명_인용_자치법규"], 1)

    def test_repeated_citation_in_one_article_counted_once(self):
        api = FakeAPI()
        api.get_ordinance_articles = lambda mst: ({}, [("지원", f"제3조(지원) 「{OLD}」 및 「{OLD}」 제15조")])
        rec_, _, s, _refs = radar_scan.scan_law(api, LAW, "6110000", log=lambda *_: None)
        self.assertEqual(s["이전명_인용_조문"], 2)          # 기관 2곳 × 제3조 1건
        self.assertEqual(len({(r["ord_id"], r["jo"]) for r in rec_}), len(rec_))

    def test_manual_old_name_is_flagged(self):
        law = {**LAW, "prev_name": "", "extra": [OLD]}      # API에 이전명이 없고 목록 파일에 직접 적은 경우
        rec_, _, s, _refs = radar_scan.scan_law(FakeAPI(), law, "6110000", log=lambda *_: None)
        self.assertEqual(s["이전명_인용_자치법규"], 2)
        self.assertTrue(all(r["cited_kind"] == "이전명(수동)" for r in rec_ if r["cited"] == OLD))

    def test_unstable_paging_is_requeried(self):
        api, calls = FakeAPI(), []
        full = api.search_ordinance_all
        def flaky(term, **k):                       # 첫 조회는 1건만, 재조회에서 2건
            calls.append(term)
            total, items = full(term, **k)
            return total, (items[:1] if len(calls) == 1 else items)
        api.search_ordinance_all = flaky
        _, _, s, _refs = radar_scan.scan_law(api, LAW, "6110000", log=lambda *_: None)
        self.assertEqual(s["대상_자치법규"], 2)
        self.assertEqual(s["수집_부족(검색어별)"], {})

    def test_department_is_carried_to_records(self):
        api = FakeAPI()
        api.get_ordinance_articles = lambda mst: ({"dept": "디지털정책과"}, [("목적", f"제1조(목적) 「{OLD}」에 따른다.")])
        rec_, _, _, _refs = radar_scan.scan_law(api, LAW, "6110000", log=lambda *_: None)
        self.assertTrue(rec_ and all(r["dept"] == "디지털정책과" for r in rec_))

    def test_cap_reached_is_reported(self):
        _, _, s, _refs = radar_scan.scan_law(FakeAPI(total=5000), LAW, "6110000", max_count=2, log=lambda *_: None)
        self.assertTrue(s["수집_상한_도달"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.st = ReviewStore(self.path)

    def tearDown(self):
        self.st.close()

    def test_same_input_twice_no_duplicate(self):
        self.assertEqual(self.st.register([rec()], "시험")["new"], 1)
        self.assertEqual(self.st.register([rec()], "시험"), {"new": 0, "dup": 1, "relinked": 0})
        self.assertEqual(len(self.st.events()), 1)

    def test_similar_names_different_ids_kept_apart(self):
        self.st.register([rec(ord_id="O1"), rec(ord_id="O2")], "시험")
        self.assertEqual(len(self.st.events()), 2)

    def test_new_law_version_after_done_links_new_event(self):
        self.st.register([rec()], "시험")
        eid = self.st.events()[0]["id"]
        for s in ("검토 중", "정비 추진"):
            self.st.update(eid, status=s)
        self.st.update(eid, status="완료", reason="명칭 정비 공포", reviewer="법무")
        cnt = self.st.register([rec(law_mst="M2")], "시험")
        self.assertEqual(cnt["relinked"], 1)
        old = self.st.get(eid)
        new = [e for e in self.st.events() if e["id"] != eid][0]
        self.assertEqual(old["status"], "완료")          # 이전 결론 보존
        self.assertEqual(new["prev_event_id"], eid)       # 새 사건과 연결
        self.assertEqual(new["status"], "신규")

    def test_close_requires_reason_and_reviewer(self):
        self.st.register([rec()], "시험")
        eid = self.st.events()[0]["id"]
        self.st.update(eid, status="검토 중")
        with self.assertRaises(ReviewError):
            self.st.update(eid, status="정비 불필요")
        with self.assertRaises(ReviewError):
            self.st.update(eid, status="완료")            # 정비 추진을 거치지 않은 완료
        self.st.update(eid, status="정비 불필요", reason="부칙 경과규정으로 효력 유지", reviewer="법무")
        self.assertEqual(self.st.get(eid)["status"], "정비 불필요")

    def test_history_and_persistence_after_restart(self):
        self.st.register([rec()], "시험")
        eid = self.st.events()[0]["id"]
        self.st.update(eid, status="검토 중", dept="디지털정책과", due="2026-11-30")
        self.st.close()
        self.st = ReviewStore(self.path)
        r = self.st.get(eid)
        self.assertEqual((r["status"], r["dept"], r["due"]), ("검토 중", "디지털정책과", "2026-11-30"))
        self.assertGreaterEqual(len(self.st.history(eid)), 4)

    def test_department_prefilled_on_register(self):
        r = rec()
        r["dept"] = "자원순환과"
        self.st.register([r, rec(ord_id="O2")], "시험")
        depts = sorted(e["dept"] for e in self.st.events())
        self.assertEqual(depts, ["", "자원순환과"])
        self.assertEqual(self.st.stats()["미배정"], 1)

    def test_rescan_fills_empty_dept_but_keeps_manual_one(self):
        self.st.register([rec(ord_id="O1"), rec(ord_id="O2")], "시험")
        e2 = [e for e in self.st.events() if e["ord_id"] == "O2"][0]
        self.st.update(e2["id"], dept="담당자가 정한 부서")
        r1, r2 = rec(ord_id="O1"), rec(ord_id="O2")
        r1["dept"] = r2["dept"] = "자원순환과"
        self.st.register([r1, r2], "시험")
        got = {e["ord_id"]: e["dept"] for e in self.st.events()}
        self.assertEqual(got, {"O1": "자원순환과", "O2": "담당자가 정한 부서"})

    def test_unassigned_stay_in_denominator(self):
        self.st.register([rec(ord_id="O1"), rec(ord_id="O2")], "시험")
        s = self.st.stats()
        self.assertEqual((s["전체"], s["미배정"], s["종결률"]), (2, 2, 0.0))

    def test_restore_rejects_broken_file_and_keeps_data(self):
        self.st.register([rec()], "시험")
        bad = os.path.join(self.dir, "bad.db")
        with open(bad, "wb") as f:
            f.write(b"not a database")
        with self.assertRaises(ReviewError):
            self.st.restore(bad)
        self.assertEqual(len(self.st.events()), 1)

    def test_backup_restore_roundtrip(self):
        self.st.register([rec()], "시험")
        bk = os.path.join(self.dir, "bk.db")
        self.st.backup(bk)
        self.st.register([rec(ord_id="O9")], "시험")
        self.st.restore(bk)
        self.assertEqual(len(self.st.events()), 1)

    def test_csv_formula_is_neutralized(self):
        r = rec()
        r["snippet"] = "=HYPERLINK(\"http://x\")"
        self.st.register([r], "시험")
        p = os.path.join(self.dir, "o.csv")
        self.st.export_csv(p)
        with open(p, encoding="utf-8-sig") as f:
            row = list(csv.reader(f))[1]
        self.assertTrue(any(c.startswith("'=") for c in row))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── 유형② 조문 인용 점검 · 대비표 초안 · 유형①③ 보조 · DB v2 ─────────────────────
import json  # noqa: E402
import sqlite3  # noqa: E402

import re  # noqa: E402

import ai_review  # noqa: E402
import revision as R  # noqa: E402

NEW, OLDN = "순환경제사회 전환 촉진법", "자원순환기본법"


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()
LAW_ARTS = [{"no": "5", "key": "5", "title": "국가 및 지방자치단체의 책무", "content": "지방자치단체는 폐기물 감량 시책을 추진하여야 한다."},
            {"no": "9", "key": "9의2", "title": "재활용 지원", "content": "재활용 지원", "moved_from": "제12조"},
            {"no": "20", "key": "20", "title": "순환경제 성과관리", "content": "조례로 정하는 바에 따라 성과를 관리한다."}]


class RevisionTests(unittest.TestCase):
    def test_refs_direct_and_short_alias(self):
        arts = [("목적", f"제1조(목적) 이 조례는 「{NEW}」(이하 \"법\"이라 한다) 제20조에 따라 정한다."),
                ("책무", f"제3조(시장의 책무) ① 시장은 「{OLDN}」 제5조제2항에 따라 시책을 추진한다."),
                ("지원", "제4조(지원) 법 제12조에 따른 재활용을 지원한다.")]
        refs = R.find_law_refs(arts, [NEW, OLDN])
        self.assertEqual([(r["ord_jo"], r["key"]) for r in refs], [("제1조", "20"), ("제3조", "5"), ("제4조", "12")])

    def test_check_refs_status_and_candidate_not_applied(self):
        refs = R.find_law_refs([("", f"제3조 「{OLDN}」 제5조에 따라"), ("", f"제4조 「{NEW}」 제12조에 따른 재활용")], [NEW, OLDN])
        res = R.check_refs(refs, LAW_ARTS, NEW)
        self.assertIn("내용 대조", res[0]["status"])                    # 옛 법령명 → 번호 있어도 내용 대조 필요
        self.assertIn("삭제·이동 의심", res[1]["status"])
        self.assertEqual(res[1]["candidate"]["label"], "제9조의2")         # 이동 이력 후보
        new, basis, checks = R.auto_type2_draft(f"제4조 「{NEW}」 제12조에 따른 재활용", NEW, [], res[1:])
        self.assertIn("제12조", new)                                       # 후보가 있어도 번호를 자동으로 바꾸지 않음
        self.assertTrue(any("제9조의2" in c for c in checks))

    def test_deleted_ref_without_candidate_is_not_removed(self):
        res = R.check_refs(R.find_law_refs([("", f"제7조 「{NEW}」 제99조에 따른 위원회")], [NEW]), LAW_ARTS, NEW)
        new, _, checks = R.auto_type2_draft(f"제7조 「{NEW}」 제99조에 따른 위원회", NEW, [], res)
        self.assertIn("제99조", new)
        self.assertTrue(any("자동 삭제하지 않음" in c for c in checks))

    def test_name_replacement_keeps_brackets_and_marks_diff(self):
        old = f"제3조(시장의 책무) ① 시장은 「{OLDN}」 제5조제2항에 따라"
        new, _, _ = R.auto_type2_draft(old, NEW, [OLDN], [])
        self.assertEqual(new, old.replace(OLDN, NEW))
        a, b = R.diff_segments(old, new)
        self.assertTrue(any(ch and NEW[:4] in t for t, ch in b))

    def test_bracketed_name_marked_as_own_unit(self):
        old = "② 자살예방센터를「정신보건법」제13조의2에 따른"
        _, b = R.diff_segments(old, old.replace("정신보건법", CUR))
        self.assertEqual([t for t, ch in b if ch], [f"「{CUR}」"])

    def test_export_has_draft_mark_and_separate_memo(self):
        d = tempfile.mkdtemp()
        p = R.export_comparison(os.path.join(d, "t.html"), "시험 조례", [("「가」 제1조", "「나」 제1조")], "출처 시험")
        html_ = _read(p)
        self.assertIn(R.DRAFT_MARK, html_)
        self.assertIn("<u>", html_)
        memo = R.build_memo("유형②", "시험 조례", "서울특별시", "자원순환과", NEW, ["근거"], "사유", ["확인"], "출처")
        for k in ("정비 유형", "근거 법령", "개정 사유", "추가 확인 사항", "출처·기준일", R.DRAFT_MARK):
            self.assertIn(k, memo)

    def test_followup_checks_for_new_article(self):
        self.assertTrue(any("신설" in c for c in R.followup_checks("<신 설>", "제9조의2(공공지능정보화) …")))


class AIHelperTests(unittest.TestCase):
    def test_obligation_and_delegation_with_subject(self):
        got = ai_review.obligation_articles(LAW_ARTS)
        self.assertEqual([(a["key"], a["kind"]) for a in got], [("5", "의무"), ("20", "위임")])
        self.assertEqual(got[0]["subject"], "지방자치단체")

    def test_parse_json_list_with_code_fence(self):
        items = ai_review.parse_json_list('설명\n```json\n[{"law_article":"제5조","judgment":"확인 필요"}]\n```')
        self.assertEqual(items[0]["judgment"], "확인 필요")
        with self.assertRaises(ValueError):
            ai_review.parse_json_list("결과 없음")


class StoreV2Tests(unittest.TestCase):
    def test_v1_database_is_migrated_without_data_loss(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "v1.db")
        con = sqlite3.connect(p)
        con.executescript("CREATE TABLE event(id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, law_name TEXT, law_id TEXT, "
                          "law_mst TEXT, law_date TEXT, ord_name TEXT, ord_id TEXT, ord_mst TEXT, gov TEXT, kind TEXT, jo TEXT, cited TEXT, "
                          "is_old INTEGER, snippet TEXT, source TEXT, fetched_at TEXT, status TEXT NOT NULL DEFAULT '신규', dept TEXT DEFAULT '', "
                          "due TEXT DEFAULT '', reason TEXT DEFAULT '', reviewer TEXT DEFAULT '', prev_event_id INTEGER, created_at TEXT, updated_at TEXT);"
                          "INSERT INTO event(event_key, ord_name, status) VALUES('k1', '옛 조례', '검토 중'); PRAGMA user_version=1;")
        con.commit(); con.close()
        st = ReviewStore(p)
        self.assertEqual(st.get_by_key("k1")["status"], "검토 중")
        self.assertEqual(st.db.execute("PRAGMA user_version").fetchone()[0], 2)
        st.close()

    def test_draft_saved_with_history(self):
        st = ReviewStore(os.path.join(tempfile.mkdtemp(), "t.db"))
        r = rec(); r["article_text"] = f"제3조 「{OLD}」에 따라"
        st.register([r], "시험")
        ev = st.events()[0]
        self.assertEqual(ev["article_text"], r["article_text"])
        st.save_draft(ev["id"], "유형②", ev["article_text"], ev["article_text"].replace(OLD, CUR), "메모")
        self.assertIn(CUR, st.get_draft(ev["id"])["draft_text"])
        self.assertTrue(any(h["field"] == "draft" for h in st.history(ev["id"])))
        st.close()


class BlindSpotScanTests(unittest.TestCase):
    def test_cross_department_citation_and_auto_draft(self):
        class API(FakeAPI):
            def search_ordinance_all(self, term, **k):
                return 2, [{"mst": "S1", "ord_id": "O1", "name": "문화유산 보호 조례", "gov": "서울특별시", "kind": "조례", "enforce": ""},
                           {"mst": "S2", "ord_id": "O2", "name": "시세 감면 조례", "gov": "서울특별시", "kind": "조례", "enforce": ""}]

            def get_ordinance_articles(self, mst):
                if mst == "S1":
                    return {"dept": "문화유산과"}, [("목적", f"제1조(목적) 이 조례는 「{CUR}」에 따른다.")]
                return {"dept": "세제과"}, [("감면", f"제3조(감면) 「{OLD}」 제15조에 따른 시설은 감면한다.")]

            def get_law_articles(self, mst):
                return [{"no": "15", "key": "15", "title": "지역지능정보화", "content": ""}]

            def search_law(self, n, display=5):
                return [{"name": CUR, "law_id": "L1", "mst": "M1", "promulgation": "2024", "kind": "법률"}]

            def get_law_meta(self, m):
                return {"prev_name": OLD, "alias": ""}

        rec_, _, s, refs = radar_scan.scan_law(API(), LAW, "6110000", log=lambda *_: None)
        cross = [r for r in rec_ if r["cross_dept"]]
        self.assertEqual([(r["ord_name"], r["dept"], r["lawside_dept"]) for r in cross], [("시세 감면 조례", "세제과", "문화유산과")])
        self.assertEqual((s["타부서_옛이름_인용_자치법규"], s["본청_인용_소관부서수"]), (1, 2))
        self.assertEqual(len(refs), 1)
        # main() 전체 흐름: 대비표 초안·메모·목록 파일 생성
        import law_ordinance_network as lon2
        orig = lon2.LawGoKrAPI
        lon2.LawGoKrAPI = lambda oc: API()
        os.environ["LAW_OC"] = "x"
        d = tempfile.mkdtemp(); lf = os.path.join(d, "l.txt")
        open(lf, "w", encoding="utf-8").write(CUR + "\n")
        try:
            res = radar_scan.main(["--laws", lf, "--out", os.path.join(d, "o"), "--db", os.path.join(d, "r.db")])
        finally:
            lon2.LawGoKrAPI = orig
        dd = os.path.join(d, "o", "대비표초안")
        files = os.listdir(dd)
        self.assertTrue(any(f.startswith("신구조문대비표_초안_") for f in files) and any(f.startswith("검토메모_") for f in files))
        memo = _read(os.path.join(dd, [f for f in files if f.startswith("검토메모_")][0]))
        self.assertIn("부서 간 정비 사각지대 후보", memo)
        self.assertEqual((res["전체"]["대비표초안_작성건수"], res["전체"]["대비표초안_타부서건수"]), (1, 1))
        html_ = _read(os.path.join(dd, [f for f in files if f.endswith(".html")][0]))
        self.assertIn(CUR, re.sub(r"<[^>]+>", "", html_))      # 개정안에 현행 법령명
        self.assertIn(f"<u><b>「{CUR}」</b></u>", html_)  # 바뀐 법령명 전체 밑줄

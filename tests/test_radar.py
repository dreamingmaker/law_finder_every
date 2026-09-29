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
        rec_, fail, s = radar_scan.scan_law(FakeAPI(fail_msts={"S2"}), LAW, "6110000", log=lambda *_: None)
        self.assertEqual(s["미검증"], 1)
        self.assertEqual(fail[0]["mst"], "S2")
        self.assertEqual(s["이전명_인용_자치법규"], 1)

    def test_repeated_citation_in_one_article_counted_once(self):
        api = FakeAPI()
        api.get_ordinance_articles = lambda mst: ({}, [("지원", f"제3조(지원) 「{OLD}」 및 「{OLD}」 제15조")])
        rec_, _, s = radar_scan.scan_law(api, LAW, "6110000", log=lambda *_: None)
        self.assertEqual(s["이전명_인용_조문"], 2)          # 기관 2곳 × 제3조 1건
        self.assertEqual(len({(r["ord_id"], r["jo"]) for r in rec_}), len(rec_))

    def test_manual_old_name_is_flagged(self):
        law = {**LAW, "prev_name": "", "extra": [OLD]}      # API에 이전명이 없고 목록 파일에 직접 적은 경우
        rec_, _, s = radar_scan.scan_law(FakeAPI(), law, "6110000", log=lambda *_: None)
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
        _, _, s = radar_scan.scan_law(api, LAW, "6110000", log=lambda *_: None)
        self.assertEqual(s["대상_자치법규"], 2)
        self.assertEqual(s["수집_부족(검색어별)"], {})

    def test_department_is_carried_to_records(self):
        api = FakeAPI()
        api.get_ordinance_articles = lambda mst: ({"dept": "디지털정책과"}, [("목적", f"제1조(목적) 「{OLD}」에 따른다.")])
        rec_, _, _ = radar_scan.scan_law(api, LAW, "6110000", log=lambda *_: None)
        self.assertTrue(rec_ and all(r["dept"] == "디지털정책과" for r in rec_))

    def test_cap_reached_is_reported(self):
        _, _, s = radar_scan.scan_law(FakeAPI(total=5000), LAW, "6110000", max_count=2, log=lambda *_: None)
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

# -*- coding: utf-8 -*-
"""
서울 일괄 점검 (서울형 조례정비 레이더 · 실측용)
==============================================
법령 목록을 받아 서울특별시와 25개 자치구 자치법규 전체에서
현행명·옛 명칭(제명변경 전)·약칭 인용을 조문 단위로 찾고, 결과를 파일로 남긴다.

  python radar_scan.py                       # 기본 법령목록(점검대상_법령.txt), 서울특별시
  python radar_scan.py --laws 목록.txt --region 서울특별시 --max 1000

산출물(실측결과_YYYYMMDD_HHMM 폴더):
  인용목록.csv   — 조문 단위 인용 전체(원문 문장·조회일 포함)
  확인필요목록.csv — 본문 조회 실패 등으로 자동 확인하지 못한 자치법규(‘인용 없음’으로 세지 않음)
  요약.json / 요약.md — 법령별·기관별 집계, 소요시간, API 호출 수, 수집 상한 도달 여부
  radar.db       — 검토카드 저장소에 변경사건으로 등록(같은 법령 버전 재실행 시 중복 0건)
인증키(OC)는 .env 의 LAW_OC 에서 읽으며 산출물에 기록하지 않는다.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

import law_ordinance_network as lon
import revision as R
from review_store import ReviewStore, csv_safe

HERE = os.path.dirname(os.path.abspath(__file__))


class CountingAPI:
    """API 호출 수를 세는 얇은 래퍼."""

    def __init__(self, api):
        self.api, self.calls = api, 0

    def __getattr__(self, name):
        fn = getattr(self.api, name)

        def wrapped(*a, **k):
            self.calls += 1
            return fn(*a, **k)
        return wrapped


def resolve_law(api, name):
    """입력 법령명 → 법률 정식명·법령ID·버전·이전명·약칭. 이름이 정확히 같은 법률을 우선."""
    laws = api.search_law(name, display=20)
    exact = [x for x in laws if x["name"] == name]
    pick = next((x for x in exact if x["kind"] == "법률"), None) or (exact[0] if exact else None) \
        or next((x for x in laws if x["kind"] == "법률"), None) or (laws[0] if laws else None)
    if not pick:
        return None
    meta, meta_err = {}, ""
    for _ in range(2):   # 이전법령명 조회 실패를 '이전명 없음'으로 넘기지 않도록 재시도 후 기록
        try:
            meta = api.get_law_meta(pick["mst"]) if pick.get("mst") else {}
            meta_err = ""
            break
        except lon.LawApiError as e:
            meta_err = str(e).splitlines()[0][:120]
    return {"name": pick["name"], "law_id": pick.get("law_id") or meta.get("law_id", ""),
            "mst": pick.get("mst", ""), "promulgation": pick.get("promulgation", ""),
            "prev_name": meta.get("prev_name", ""), "alias": meta.get("alias", ""), "meta_error": meta_err}


def scan_law(api, law, org, max_count=1000, log=print):
    """법령 1건 점검 → (인용 레코드, 미검증 목록, 집계)."""
    extra = law.get("extra", [])
    terms = lon.collect_law_terms(law["name"], law["prev_name"], law["alias"], ",".join(extra))
    kinds = {law["name"]: "현행명"}
    if law["prev_name"]:
        kinds[law["prev_name"]] = "이전명"
    for e in extra:                      # 법령목록 파일에 직접 적은 옛 이름
        kinds.setdefault(e, "이전명(수동)")
    if law["alias"]:
        kinds.setdefault(law["alias"], "약칭")

    merged, hits_by_term, capped, short = {}, {}, False, {}
    for t in terms:
        got = {}
        for _ in range(3):   # 페이지 순서가 흔들리면 한 번에 다 못 받을 수 있어 부족분을 재조회(최대 3회)
            total, items = api.search_ordinance_all(t, org=org, search=2, max_count=max_count)
            for it in items:
                got.setdefault(it["mst"], it)
            if len(got) >= min(total, max_count):
                break
        hits_by_term[t] = total
        capped = capped or total > max_count
        if len(got) < min(total, max_count):
            short[t] = min(total, max_count) - len(got)
        for mst, it in got.items():
            merged.setdefault(mst, it)
    log("  검색: " + ", ".join(f"{t} {n}건" for t, n in hits_by_term.items()) + f" → 자치법규 {len(merged)}건")

    try:   # 유형② 조문 인용 점검용 현행 조문 목록(법령당 1회 조회)
        law_arts = api.get_law_articles(law["mst"]) if law.get("mst") else []
    except Exception:
        law_arts = []
    records, failed, seen, ref_rows = [], [], set(), []
    for i, it in enumerate(merged.values(), 1):
        try:
            meta, arts = api.get_ordinance_articles(it["mst"])
        except Exception as e:  # 조회 실패는 '인용 없음'이 아니라 '미검증'
            failed.append({**it, "error": str(e).splitlines()[0][:120]})
            continue
        if not arts:
            failed.append({**it, "error": "조문 없음(지원하지 않는 원문 구조)"})
            continue
        dept = (meta or {}).get("dept", "")
        if law_arts:
            for r in R.check_refs(R.find_law_refs(arts, terms), law_arts, law["name"]):
                ref_rows.append({**r, "law_name": law["name"], "gov": it["gov"], "dept": dept, "ord_name": it["name"],
                                 "ord_id": it["ord_id"], "ord_mst": it["mst"]})
        for c in lon.find_citations(arts, terms):
            k = (it["ord_id"], c["jo"], c["term"])   # 같은 조문 안 반복 인용·같은 조례의 다른 버전은 1건
            if k in seen:
                continue
            seen.add(k)
            records.append({
                "law_name": law["name"], "law_id": law["law_id"], "law_mst": law["mst"],
                "law_date": law["promulgation"], "ord_name": it["name"], "ord_id": it["ord_id"],
                "ord_mst": it["mst"], "gov": it["gov"], "dept": dept, "article_text": c.get("text", ""),
                "kind": it["kind"], "enforce": it["enforce"],
                "jo": c["jo"], "cited": c["term"], "cited_kind": kinds.get(c["term"], "추가어"),
                "is_old": kinds.get(c["term"], "").startswith("이전명"), "purpose": c["purpose"], "snippet": c["snippet"],
            })
        if i % 20 == 0:
            log(f"  본문 확인 {i}/{len(merged)}")

    # 부서 간 정비 사각지대 후보: 같은 기관에서 이 법을 제1조(목적) 근거로 삼는 조례의 소관부서를 ‘법령 담당 부서(추정)’로 보고,
    # 그 밖의 부서 소관 조례에서 이 법을 인용한 경우를 표시한다(추정 — 담당자 확인 대상).
    lawside = defaultdict(set)
    for r in records:
        if r["purpose"] and r["dept"]:
            lawside[r["gov"]].add(r["dept"])
    for r in records:
        r["lawside_dept"] = ", ".join(sorted(lawside[r["gov"]]))
        r["cross_dept"] = bool(r["dept"] and lawside[r["gov"]] and r["dept"] not in lawside[r["gov"]])
    old = [r for r in records if r["is_old"]]
    citing = {r["ord_id"] for r in records}
    seoul = [r for r in records if r["gov"] == "서울특별시"]
    summary = {
        "법령": law["name"], "법령ID": law["law_id"], "법령버전(MST)": law["mst"], "공포일": law["promulgation"],
        "이전명": ", ".join([law["prev_name"]] * bool(law["prev_name"]) + [e for e in extra if e != law["prev_name"]]),
        "이전명_API": law["prev_name"], "이전명_조회오류": law.get("meta_error", ""), "약칭": law["alias"], "검색어별_검색건수": hits_by_term,
        "수집_상한_도달": capped, "수집_부족(검색어별)": short, "대상_자치법규": len({x["ord_id"] for x in merged.values()}), "본문확인": len(merged) - len(failed),
        "미검증": len(failed), "인용_자치법규": len(citing), "인용_조문": len(records),
        "이전명_인용_조문": len(old), "이전명_인용_자치법규": len({r["ord_id"] for r in old}),
        "이전명_인용_기관": dict(Counter(r["gov"] for r in {r["ord_id"]: r for r in old}.values())),
        "목적조항_근거": len({r["ord_id"] for r in records if r["purpose"]}),
        "본청_인용_자치법규": len({r["ord_id"] for r in seoul}),
        "본청_인용_소관부서수": len({r["dept"] for r in seoul if r["dept"]}),
        "본청_법령담당부서(추정)": sorted(lawside["서울특별시"]),
        "타부서_인용_자치법규": len({r["ord_id"] for r in records if r["cross_dept"]}),
        "타부서_옛이름_인용_자치법규": len({r["ord_id"] for r in old if r["cross_dept"]}),
        "소관부서_미확인_자치법규": len({r["ord_id"] for r in records if not r["dept"]}),
        "조문인용_점검건수": len(ref_rows),
        "현행에_없는_조문_인용": sum(1 for r in ref_rows if "없음" in r["status"]),
        "옛이름_조문번호_대조필요": sum(1 for r in ref_rows if "내용 대조" in r["status"]),
        "현행조문목록_조회": bool(law_arts),
    }
    return records, failed, summary, ref_rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="서울 일괄 점검 — 옛 법령명 인용 실측")
    ap.add_argument("--laws", default=os.path.join(HERE, "점검대상_법령.txt"))
    ap.add_argument("--region", default="서울특별시")
    ap.add_argument("--max", type=int, default=1000, help="검색어별 최대 수집 건수")
    ap.add_argument("--out", default="")
    ap.add_argument("--db", default=os.path.join(HERE, "radar.db"))
    a = ap.parse_args(argv)

    oc = os.environ.get("LAW_OC", "").strip()
    if not oc:
        sys.exit("LAW_OC(법제처 OPEN API 인증키)가 없습니다. .env 파일에 LAW_OC=... 를 넣으세요.")
    sido, org, _ = lon.normalize_region(a.region)
    if not org:
        sys.exit(f"지역을 알 수 없습니다: {a.region}")
    with open(a.laws, encoding="utf-8-sig") as f:
        lines = [s.strip() for s in f if s.strip() and not s.lstrip().startswith("#")]
    # 한 줄 형식: 현행 법령명 | 옛 이름1, 옛 이름2   (옛 이름은 선택 — API 이전법령명에 없을 때 직접 보탬)
    names = [(ln.split("|")[0].strip(), [x.strip() for x in ln.split("|")[1].split(",") if x.strip()] if "|" in ln else [])
             for ln in lines]
    out = a.out or os.path.join(HERE, "실측결과_" + datetime.now().strftime("%Y%m%d_%H%M"))
    os.makedirs(out, exist_ok=True)

    api = CountingAPI(lon.LawGoKrAPI(oc))
    store = ReviewStore(a.db)
    started = datetime.now()
    t0 = time.time()
    all_rec, all_fail, per_law, all_refs = [], [], [], []
    for n, extra in names:
        print(f"■ {n}" + (f"  (+옛 이름 {', '.join(extra)})" if extra else ""))
        t1 = time.time()
        try:
            law = resolve_law(api, n)
        except lon.LawApiError as e:
            sys.exit(str(e))
        if not law:
            print("  법령을 찾지 못함 — 건너뜀")
            per_law.append({"법령": n, "오류": "법령 검색 결과 없음"})
            continue
        law["extra"] = extra
        if law.get("meta_error"):
            print("  ⚠ 이전 법령명 조회 실패(목록 파일의 옛 이름만 사용): " + law["meta_error"])
        if not law["prev_name"] and not extra:
            print("  이전 법령명 없음(제명변경 이력 없음) — 현행명·약칭만 점검")
        rec, fail, s, refs = scan_law(api, law, org, a.max)
        all_refs += refs
        cnt = store.register(rec, "API", law_name=law["name"], scope=sido, n_failed=len(fail),
                             capped=s["수집_상한_도달"])
        s["검토카드_등록"] = cnt
        s["소요초"] = round(time.time() - t1, 1)
        print(f"  인용 {s['인용_조문']}조문/{s['인용_자치법규']}건 · 이전명 인용 {s['이전명_인용_자치법규']}건 · "
              f"미검증 {s['미검증']} · {s['소요초']}초")
        all_rec += rec
        all_fail += [{**x, "law_name": law["name"]} for x in fail]
        per_law.append(s)

    with open(os.path.join(out, "인용목록.csv"), "w", encoding="utf-8-sig", newline="") as f:
        cols = ["law_name", "gov", "dept", "cross_dept", "lawside_dept", "ord_name", "kind", "jo", "cited", "cited_kind", "purpose", "snippet",
                "enforce", "ord_id", "ord_mst", "law_mst"]
        w = csv.writer(f)
        w.writerow(["법령", "지자체", "소관부서", "타부서 인용(추정)", "법령 담당부서(추정)", "자치법규명", "종류", "인용조문", "인용명칭", "명칭구분", "목적조항",
                    "인용문장", "시행일", "자치법규ID", "자치법규MST", "법령MST"])
        for r in all_rec:
            w.writerow([csv_safe(r[c]) for c in cols])
    with open(os.path.join(out, "확인필요목록.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["법령", "지자체", "자치법규명", "자치법규MST", "사유"])
        for r in all_fail:
            w.writerow([csv_safe(x) for x in (r["law_name"], r["gov"], r["name"], r["mst"], r["error"])])

    with open(os.path.join(out, "조문인용점검.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["법령", "지자체", "소관부서", "자치법규명", "조례 조문", "인용", "판정", "현행 조문 제목", "대체 후보(확인 필요)"])
        for r in all_refs:
            c = r.get("candidate")
            w.writerow([csv_safe(x) for x in (r["law_name"], r["gov"], r["dept"], r["ord_name"], r["ord_jo"], r["text"], r["status"],
                                             r.get("current_title", ""), f"{c['label']} {c['title']} ({c['why']})" if c else "")])

    # 유형② 대비표 초안 자동 작성: 옛 법령명 인용 조문 + 현행 법에 없는 조문을 인용한 조문
    ddir = os.path.join(out, "대비표초안"); os.makedirs(ddir, exist_ok=True)
    targets = {}
    for r in all_rec:
        if r["is_old"]:
            targets.setdefault((r["law_name"], r["ord_mst"], r["jo"]), {"rec": r, "cited": set()})["cited"].add(r["cited"])
    for r in all_refs:
        if "없음" in r["status"]:
            rec = next((x for x in all_rec if x["ord_mst"] == r["ord_mst"] and x["jo"] == r["ord_jo"] and x["law_name"] == r["law_name"]), None)
            if rec:
                targets.setdefault((r["law_name"], r["ord_mst"], r["ord_jo"]), {"rec": rec, "cited": set()})
    t_draft = time.time()
    index = []
    for (law_name, mst, jo), v in targets.items():
        r = v["rec"]
        refs = [x for x in all_refs if x["ord_mst"] == mst and x["ord_jo"] == jo and x["law_name"] == law_name]
        new, basis, checks = R.auto_type2_draft(r["article_text"], law_name, sorted(v["cited"]), refs)
        base = R.safe_filename(r["gov"], r["ord_name"], jo)
        srcn = f"현행 조문 출처: 법제처 국가법령정보 공동활용, 조회 {started:%Y-%m-%d %H:%M}, 자치법규 MST {mst}"
        R.export_comparison(os.path.join(ddir, f"신구조문대비표_초안_{base}.html"), r["ord_name"], [(r["article_text"], new)], srcn)
        reason = (f"상위법 명칭 변경(「{'」·「'.join(sorted(v['cited']))}」 → 「{law_name}」)에 따른 인용 정비" if v["cited"]
                  else "상위법 조문 삭제·이동 의심에 따른 인용 확인")
        memo = R.build_memo("유형② 변경된 법령명·조문 인용 정비", r["ord_name"], r["gov"], r["dept"], law_name, basis, reason,
                            list(dict.fromkeys(checks + (["부서 간 정비 사각지대 후보: 법령 담당부서(추정) " + r["lawside_dept"] + "와 소관부서가 다름"]
                                                         if r["cross_dept"] else []))), srcn)
        with open(os.path.join(ddir, f"검토메모_{base}.txt"), "w", encoding="utf-8") as f:
            f.write(memo)
        index.append([law_name, r["gov"], r["dept"], "예" if r["cross_dept"] else "", r["lawside_dept"], r["ord_name"], jo,
                      "·".join(sorted(v["cited"])) or "-", len(refs), sum(1 for x in refs if "없음" in x["status"])])
    draft_sec = round(time.time() - t_draft, 2)
    with open(os.path.join(ddir, "대비표초안_목록.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["법령", "지자체", "소관부서", "타부서 인용(추정)", "법령 담당부서(추정)", "자치법규명", "조문", "옛 법령명", "조문인용 수", "현행에 없는 조문 인용"])
        w.writerows([[csv_safe(x) for x in row] for row in index])

    src = os.path.join(HERE, "law_ordinance_network.py")
    old_by_gov = defaultdict(int)
    for s in per_law:
        for g, k in (s.get("이전명_인용_기관") or {}).items():
            old_by_gov[g] += k
    total = {
        "실행일시": started.strftime("%Y-%m-%d %H:%M"), "지역": sido, "법제처_OPEN_API": True,
        "점검_법령수": len(names), "제명변경_법령수": sum(1 for s in per_law if s.get("이전명")),
        "대상_자치법규(법령별 합)": sum(s.get("대상_자치법규", 0) for s in per_law),
        "인용_자치법규(법령별 합)": sum(s.get("인용_자치법규", 0) for s in per_law),
        "이전명_인용_자치법규(법령별 합)": sum(s.get("이전명_인용_자치법규", 0) for s in per_law),
        "이전명_인용_조문": sum(s.get("이전명_인용_조문", 0) for s in per_law),
        "이전명_인용_기관수": len(old_by_gov), "미검증": len(all_fail),
        "수집_상한_도달_법령": [s["법령"] for s in per_law if s.get("수집_상한_도달")],
        "타부서_인용_자치법규(법령별 합)": sum(s.get("타부서_인용_자치법규", 0) for s in per_law),
        "타부서_옛이름_인용_자치법규(법령별 합)": sum(s.get("타부서_옛이름_인용_자치법규", 0) for s in per_law),
        "조문인용_점검건수": len(all_refs), "현행에_없는_조문_인용": sum(1 for r in all_refs if "없음" in r["status"]),
        "대비표초안_작성건수": len(index), "대비표초안_타부서건수": sum(1 for x in index if x[3]), "대비표초안_생성초": draft_sec,
        "총_소요초": round(time.time() - t0, 1), "API_호출수": api.calls,
        "프로그램_sha256": hashlib.sha256(open(src, "rb").read()).hexdigest(),
    }
    result = {"전체": total, "법령별": per_law, "이전명_인용_기관별": dict(old_by_gov)}
    with open(os.path.join(out, "요약.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    md = [f"# 서울 일괄 점검 결과 ({total['실행일시']}, 법제처 OPEN API)", "",
          "| 법령 | 이전명 | 대상 | 인용 | 이전명 인용(건/조문) | 미검증 | 상한 | 초 |", "|---|---|---|---|---|---|---|---|"]
    for s in per_law:
        if "오류" in s:
            md.append(f"| {s['법령']} | - | - | - | - | - | - | {s['오류']} |")
            continue
        md.append(f"| {s['법령']} | {s['이전명'] or '-'} | {s['대상_자치법규']} | {s['인용_자치법규']} | "
                  f"{s['이전명_인용_자치법규']}/{s['이전명_인용_조문']} | {s['미검증']} | "
                  f"{'도달' if s['수집_상한_도달'] else '-'} | {s['소요초']} |")
    md += ["", "| 법령 | 본청 인용 자치법규 | 본청 소관부서 수 | 본청 법령 담당부서(추정) | 타부서 인용 | 타부서 옛 이름 인용 | 현행에 없는 조문 인용 |",
           "|---|---|---|---|---|---|---|"]
    for s in per_law:
        if "오류" not in s:
            md.append(f"| {s['법령']} | {s['본청_인용_자치법규']} | {s['본청_인용_소관부서수']} | {', '.join(s['본청_법령담당부서(추정)']) or '-'} | "
                      f"{s['타부서_인용_자치법규']} | {s['타부서_옛이름_인용_자치법규']} | {s['현행에_없는_조문_인용']} |")
    md += ["", "```json", json.dumps(total, ensure_ascii=False, indent=2), "```"]
    with open(os.path.join(out, "요약.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    store.close()
    print(f"\n완료: {out}\n  이전명 인용 자치법규 {total['이전명_인용_자치법규(법령별 합)']}건 "
          f"({total['이전명_인용_기관수']}개 기관) · 미검증 {total['미검증']} · {total['총_소요초']}초")
    return result


if __name__ == "__main__":
    main()

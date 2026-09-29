# -*- coding: utf-8 -*-
"""
생성형 AI 검토 (유형① 의무·위임 반영 여부 / 유형③ 취지·현실 적합성 — 시범)
============================================================================
결과는 JSON 목록으로 받아 화면 표에 보여주고, 담당자가 ‘채택’한 항목만 대비표 초안으로 넘긴다.
AI 결과는 검토 후보일 뿐이며, 법적 오류·개정 의무로 확정 표시하지 않는다.
"""

import json
import re

import requests

from revision import law_article_label

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"

OBLIG_RE = re.compile(r"하여야 한다|해야 한다")
DELEG_RE = re.compile(r"조례로 정한다|조례로 정하는|조례로 정할 수|조례로 정하여야")
LOCAL_RE = re.compile(r"지방자치단체|국가기관등|시ㆍ도지사|시·도지사|시장ㆍ군수ㆍ구청장|시장·군수·구청장|구청장")


def _subject(content):
    body = re.sub(r"^\s*제\d+조(?:의\d+)?\s*(?:\([^)]*\))?\s*", "", content or "")
    body = re.sub(r"^[①-⑳]\s*", "", body)
    m = re.match(r"(.{1,25}?)(?:은|는)\s", body)
    return m.group(1).strip() if m else ""


def obligation_articles(law_articles):
    """AI에 보내기 전 규칙으로 고르는 후보:
    · 위임: ‘조례로 정한다’ 등 조례 위임 문구가 있는 조문
    · 의무: ‘하여야 한다’ + 지자체(국가기관등 포함)가 적힌 조문
    각 항목에 kind(위임/의무)·subject(주체 추정)를 덧붙인다. ‘하여야 한다’만으로 개정 필요를 판정하지 않는다."""
    out = []
    for a in law_articles:
        c = a.get("content") or ""
        if DELEG_RE.search(c):
            out.append({**a, "kind": "위임", "subject": _subject(c)})
        elif OBLIG_RE.search(c) and LOCAL_RE.search(c):
            out.append({**a, "kind": "의무", "subject": _subject(c)})
    return out


def parse_json_list(text):
    """AI 응답에서 JSON 배열만 꺼낸다(코드블록·앞뒤 설명 허용). 실패하면 ValueError."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M)
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j < i:
        raise ValueError("AI 응답에서 결과 목록(JSON)을 찾지 못했습니다.")
    data = json.loads(t[i:j + 1])
    if not isinstance(data, list):
        raise ValueError("AI 응답 형식이 목록이 아닙니다.")
    return [d for d in data if isinstance(d, dict)]


_COMMON = ("원칙: 1) 주어진 원문·자료만 근거로 한다. 2) 목록에 없는 조문번호를 만들지 않는다. "
           "3) 확신이 없으면 '확인 필요'. 4) 위법 여부나 개정 의무를 확정하지 않는다(검토 후보로만 제시). "
           "5) 근거 없는 정책 방향·개정 사유를 지어내지 않는다. 출력은 JSON 배열만.")

TYPE1_SYSTEM = (
    "당신은 지방자치단체 조례 담당자를 돕는 검토 보조자다. 상위법의 의무·위임 조항마다 조례에 반영할 필요가 있는지 검토 후보를 만든다.\n"
    "판단 원칙: '하여야 한다'는 표현만으로 개정이 필요하다고 판정하지 않는다. "
    "법률이 직접 적용되는 사항인지, 조례로 정하도록 위임한 사항인지, 다른 조례에 이미 반영됐을 수 있는지 구분한다.\n"
    + _COMMON + "\n각 원소: {\"law_article\":\"제N조\",\"kind\":\"의무|위임\",\"subject\":\"주체\",\"target\":\"적용 대상\","
    "\"ord_article\":\"대응 조례 조문(제N조) 또는 '없음'\",\"judgment\":\"반영됨|검토 후보|확인 필요\","
    "\"reason\":\"이유(한두 문장)\",\"check\":\"담당자가 확인할 사항\",\"draft\":\"검토 후보일 때만 신설·수정 문안 초안, 아니면 빈 문자열\"}")

TYPE3_SYSTEM = (
    "당신은 지방자치단체 조례 담당자를 돕는 검토 보조자다. 조례 내용이 상위법 취지나 담당자가 제공한 정책·현장 자료에 비추어 "
    "보완을 검토할 만한지 후보를 제시한다. 법적 오류나 개정 의무로 표현하지 않는다. "
    "현실 적합성을 판단할 자료가 부족하면 data_gap에 그 사실을 적는다.\n"
    + _COMMON + "\n각 원소: {\"ord_article\":\"제N조 또는 '신설'\",\"reason\":\"보완 검토 이유\","
    "\"evidence\":\"근거(법 조문 번호 또는 제공 자료명과 해당 내용 요약)\",\"judge\":\"담당자가 추가로 판단할 사항\","
    "\"data_gap\":\"자료 부족 사항 또는 빈 문자열\",\"draft\":\"선택 가능한 개정 문안 초안(없으면 빈 문자열)\"}")


def _call(api_key, system, user, model=OPENAI_MODEL):
    payload = {"model": model, "temperature": 0.1,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = requests.post(OPENAI_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json=payload, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI API 오류 {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"].strip()


def _ord_text(ord_articles, limit=400):
    return "\n".join(f"[{t or '조문'}] {c.replace(chr(10), ' ')[:limit]}" for t, c in ord_articles)


def review_type1(api_key, law_name, law_enforce, items, ord_name, gov, ord_articles):
    law_txt = "\n".join(f"제{law_article_label(a)}({a.get('title', '')}) [{a['kind']}, 주체 추정: {a['subject'] or '-'}] "
                        f"{(a.get('content') or '').replace(chr(10), ' ')[:400]}" for a in items)
    user = (f"상위법: 「{law_name}」 (시행일 {law_enforce or '미확인'})\n조례: {ord_name} ({gov})\n\n"
            f"[상위법 의무·위임 조항]\n{law_txt}\n\n[조례 전체 조문]\n{_ord_text(ord_articles)}")
    return parse_json_list(_call(api_key, TYPE1_SYSTEM, user))


def review_type3(api_key, law_name, law_items, ord_name, gov, ord_articles, materials):
    """materials: [(자료명, 본문)] — 담당자가 제공한 정책·현장 자료(없으면 빈 목록)."""
    law_txt = "\n".join(f"제{law_article_label(a)}({a.get('title', '')}) {(a.get('content') or '').replace(chr(10), ' ')[:300]}"
                        for a in law_items[:40])
    mat = "\n\n".join(f"<자료: {n}>\n{b[:4000]}" for n, b in materials) or "(제공 자료 없음 — 현실 적합성 판단 자료 부족)"
    user = (f"상위법: 「{law_name}」\n조례: {ord_name} ({gov})\n\n[상위법 주요 조문]\n{law_txt}\n\n"
            f"[조례 전체 조문]\n{_ord_text(ord_articles)}\n\n[담당자 제공 자료]\n{mat}")
    return parse_json_list(_call(api_key, TYPE3_SYSTEM, user))

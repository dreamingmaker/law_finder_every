# -*- coding: utf-8 -*-
"""
법률-조례 연결 확인기 (법률네트워크)
=====================================

「공무원을 위한 AI 활용」 7-5 ‘그래프DB로 법령 관계 분석하기’ 의
  · 6장 "법령 인용 네트워크 만들기"
  · 7장 "나를 인용하는 법 찾기"
개념을 GUI로 구현한 도구입니다.

입력 세 가지(법률명 · 지자체 이름 · 법제처 OPEN API 인증키)만 넣으면,
그 법률을 **상위법으로 인용·위임받은** 해당 지자체의 조례·규칙을 찾아
표와 네트워크 그래프로 보여 줍니다. (= "나를 인용하는 법 찾기"의 역방향 질의)

법제처 국가법령정보 공동활용 OPEN API(www.law.go.kr/DRF)를 직접 호출합니다.
  · 법령 검색   : target=law
  · 자치법규 검색: target=ordin  (search=2 → 본문 인용 검색)
  · 자치법규 본문: lawService.do target=ordin
모든 응답은 type=XML + UTF-8 로 받습니다. (JSON 응답은 한글이 깨지는 알려진 버그가 있어 사용하지 않음)

CoVe(검증의 사슬) 원칙대로, 검색 결과를 그대로 믿지 않고
각 조례 본문 제1조(목적)에 실제로 「법률명」이 인용돼 있는지 원문을 다시 대조해
연결 강도를 ‘근거(제1조 위임) / 인용(본문) / 단순매칭’ 으로 구분합니다.

작성: 2026-06-10
"""

import csv
import os
import re
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import requests

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# ──────────────────────────────────────────────────────────────────────────
# matplotlib (네트워크 그래프) — tkinter 임베드 + 한글 폰트
# ──────────────────────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("TkAgg")
from matplotlib import rcParams
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import networkx as nx

# Windows/공통 한글 폰트 지정 (없으면 기본값)
for _f in ("Malgun Gothic", "맑은 고딕", "NanumGothic", "AppleGothic", "Gulim"):
    try:
        rcParams["font.family"] = _f
        break
    except Exception:
        continue
rcParams["axes.unicode_minus"] = False


# ──────────────────────────────────────────────────────────────────────────
# 환경변수 / .env 로딩 — API 인증키(OC)를 코드에 하드코딩하지 않기 위함
#   실행 폴더(또는 exe·스크립트 폴더)에 .env 가 있으면 LAW_OC 등을 읽어 온다.
# ──────────────────────────────────────────────────────────────────────────
def _load_dotenv():
    bases = []
    try:
        bases.append(os.getcwd())
        bases.append(os.path.dirname(
            sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)))
    except Exception:
        pass
    seen = set()
    for base in bases:
        path = os.path.join(base, ".env")
        if path in seen:
            continue
        seen.add(path)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except FileNotFoundError:
            continue
        except Exception:
            continue


_load_dotenv()
DEFAULT_OC = os.environ.get("LAW_OC", "")   # .env 또는 시스템 환경변수에서 (없으면 빈 값)
DEFAULT_OPENAI = os.environ.get("OPENAI_API_KEY", "")  # 조례 개정안 생성용(선택)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"


def generate_amendment(api_key, ordinance_name, gov, old_name, new_name, cited_articles,
                       law_articles=None, model=OPENAI_MODEL):
    """옛 법령명을 인용하는 조문(cited_articles)을 OpenAI로 분석해 신구대조표(개정안)를 생성.
    law_articles(현행법 조문 번호·제목·이동이력)가 있으면, 조례가 인용한 '제N조'가
    현행법에 유효한지(삭제·이동됐는지)까지 점검한다 — 7-5-4 '끊어진 참조 찾기'.
    원칙: 원문에 있는 것만·명칭치환만·목록에 없는 조문번호 환각금지."""
    blocks = "\n\n".join(f"[{(t or '조문')}] {c}" for t, c in cited_articles)
    law_list = ""
    if law_articles:
        rows = []
        for a in law_articles:
            body = (a.get("content") or "").replace("\n", " ")
            body = re.sub(r"^제\d+조(?:의\d+)?\s*\([^)]*\)\s*", "", body)[:100]  # 조번호·제목 떼고 실내용 앞부분
            rows.append(f"제{a['no']}조({a.get('title', '')}) {body}")
        law_list = "\n".join(rows)
    system = (
        "당신은 한국 지방자치단체의 자치법규 정비 전문가다. "
        "조례 본문의 '옛 법령명'을 '현행 법령명'으로 바꾸고, 인용한 조문 번호가 현행법에서 맞는지까지 점검해 신구대조표를 만든다.\n"
        "원칙:\n"
        "1) 원문에 옛 법령명이 실제로 들어 있는 조문만 대상. 없는 문장을 지어내지 마라.\n"
        "2) 법령명은 명칭 치환. 그 외 내용·금액·날짜 변경 금지. 낫표(「」)·조사는 보존.\n"
        "3) [조문 참조 점검 — 가장 중요] 조례가 인용한 '제N조'를 **현행법 '제N조'의 실제 내용**과 비교한다. "
        "번호가 같아도 '내용'이 다르면(개정·재배치로 조문이 이동·삭제된 것이다), "
        "조례가 인용하려던 그 내용이 '현행법 조문 목록(내용 포함)'에서 몇 조에 있는지 찾아 '제N조 → 제M조'로 바꾸라고 제시한다. "
        "내용이 일치하면 '제N조 유효'. 목록 어디에도 그 내용이 없으면 '제N조 삭제 — 확인 필요'. "
        "**단순히 번호만 같다고 유효로 판단하지 말고, 목록에 없는 번호를 지어내지 마라.**\n\n"
        "출력 형식(이외 금지):\n"
        "| 조례 조문 | 인용(현행) | 개정안 | 조문 점검 | 사유 |\n"
        "각 행은 옛 법령명이 나온 조례 조문 1개씩. '개정안'에는 **법령명과 조문번호를 모두 고친** 문장을, "
        "'조문 점검'에는 '제N조 유효' / '제N조→제M조(내용 이동)' / '제N조 삭제-확인필요' 중 하나.\n"
        "표 다음 줄에 '정비 요약: ...' 한 줄."
    )
    user = (
        f"조례명: {ordinance_name} ({gov})\n"
        f"명칭 변경: 「{old_name}」 → 「{new_name}」\n\n"
        f"[옛 법령명이 인용된 조례 조문 원문]\n{blocks}\n\n"
        + (f"[현행 「{new_name}」 조문 목록(번호·제목·내용 요약)]\n{law_list}\n\n" if law_list else "")
        + f"위 조례 조문의 「{old_name}」을 「{new_name}」으로 바꾸고, "
          f"인용한 제N조가 현행법에서 **내용상** 유효한지(다르면 그 내용이 있는 제M조로) 점검한 신구대조표를 만들어줘."
    )
    payload = {"model": model, "temperature": 0.1,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    resp = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API 오류 {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


# ──────────────────────────────────────────────────────────────────────────
# 상수 : 광역자치단체 17개 → 법제처 org(지자체) 코드
#   org=광역코드 로 검색하면 산하 시·군·구 자치법규까지 모두 포함된다. (검증 완료)
# ──────────────────────────────────────────────────────────────────────────
SIDO_CODE = {
    "서울특별시": "6110000", "부산광역시": "6260000", "대구광역시": "6270000",
    "인천광역시": "6280000", "광주광역시": "6290000", "대전광역시": "6300000",
    "울산광역시": "6310000", "세종특별자치시": "5690000", "경기도": "6410000",
    "강원특별자치도": "6530000", "충청북도": "6430000", "충청남도": "6440000",
    "전북특별자치도": "6540000", "전라남도": "6460000", "경상북도": "6470000",
    "경상남도": "6480000", "제주특별자치도": "6500000",
}
# 사용자가 줄여 쓰는 별칭 → 정식 명칭
SIDO_ALIAS = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "세종시": "세종특별자치시", "경기": "경기도", "강원": "강원특별자치도", "강원도": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도", "전라북도": "전북특별자치도",
    "전남": "전라남도", "경북": "경상북도", "경남": "경상남도", "제주": "제주특별자치도",
    "제주도": "제주특별자치도",
}

# 광역자치단체별 시·군·구 (법제처 지자체기관명 표기와 일치). 세종은 단층제라 하위 없음.
SIGUNGU_ALL = "(전체)"
SIGUNGU = {
    "서울특별시": ["종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구",
                "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구", "양천구", "강서구",
                "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구"],
    "부산광역시": ["중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구", "북구", "해운대구",
                "사하구", "금정구", "강서구", "연제구", "수영구", "사상구", "기장군"],
    "대구광역시": ["중구", "동구", "서구", "남구", "북구", "수성구", "달서구", "달성군", "군위군"],
    "인천광역시": ["중구", "동구", "미추홀구", "연수구", "남동구", "부평구", "계양구", "서구", "강화군", "옹진군"],
    "광주광역시": ["동구", "서구", "남구", "북구", "광산구"],
    "대전광역시": ["동구", "중구", "서구", "유성구", "대덕구"],
    "울산광역시": ["중구", "남구", "동구", "북구", "울주군"],
    "세종특별자치시": [],
    "경기도": ["수원시", "성남시", "의정부시", "안양시", "부천시", "광명시", "평택시", "동두천시", "안산시",
             "고양시", "과천시", "구리시", "남양주시", "오산시", "시흥시", "군포시", "의왕시", "하남시",
             "용인시", "파주시", "이천시", "안성시", "김포시", "화성시", "광주시", "양주시", "포천시",
             "여주시", "연천군", "가평군", "양평군"],
    "강원특별자치도": ["춘천시", "원주시", "강릉시", "동해시", "태백시", "속초시", "삼척시", "홍천군", "횡성군",
                  "영월군", "평창군", "정선군", "철원군", "화천군", "양구군", "인제군", "고성군", "양양군"],
    "충청북도": ["청주시", "충주시", "제천시", "보은군", "옥천군", "영동군", "증평군", "진천군", "괴산군", "음성군", "단양군"],
    "충청남도": ["천안시", "공주시", "보령시", "아산시", "서산시", "논산시", "계룡시", "당진시", "금산군",
             "부여군", "서천군", "청양군", "홍성군", "예산군", "태안군"],
    "전북특별자치도": ["전주시", "군산시", "익산시", "정읍시", "남원시", "김제시", "완주군", "진안군", "무주군",
                  "장수군", "임실군", "순창군", "고창군", "부안군"],
    "전라남도": ["목포시", "여수시", "순천시", "나주시", "광양시", "담양군", "곡성군", "구례군", "고흥군", "보성군",
             "화순군", "장흥군", "강진군", "해남군", "영암군", "무안군", "함평군", "영광군", "장성군", "완도군", "진도군", "신안군"],
    "경상북도": ["포항시", "경주시", "김천시", "안동시", "구미시", "영주시", "영천시", "상주시", "문경시", "경산시",
             "의성군", "청송군", "영양군", "영덕군", "청도군", "고령군", "성주군", "칠곡군", "예천군", "봉화군", "울진군", "울릉군"],
    "경상남도": ["창원시", "진주시", "통영시", "사천시", "김해시", "밀양시", "거제시", "양산시", "의령군", "함안군",
             "창녕군", "고성군", "남해군", "하동군", "산청군", "함양군", "거창군", "합천군"],
    "제주특별자치도": ["제주시", "서귀포시"],
}

API_BASE = "http://www.law.go.kr/DRF"
DETAIL_URL = "https://www.law.go.kr/DRF/lawService.do?OC={oc}&target=ordin&MST={mst}&type=HTML"


def normalize_region(text):
    """'서울특별시 강남구' → (시도정식명, org코드, '강남구').
    광역명을 못 찾으면 (None, None, 원본텍스트)."""
    text = (text or "").strip()
    if not text:
        return None, None, ""
    # 1) 정식 명칭이 통째로 들어 있으면 우선 매칭(긴 이름 우선)
    for full in sorted(SIDO_CODE, key=len, reverse=True):
        if text.startswith(full) or full in text:
            rest = text.replace(full, "", 1).strip()
            return full, SIDO_CODE[full], rest
    # 2) 별칭 매칭
    for alias in sorted(SIDO_ALIAS, key=len, reverse=True):
        if text.startswith(alias):
            full = SIDO_ALIAS[alias]
            rest = text[len(alias):].strip()
            return full, SIDO_CODE[full], rest
    # 3) 광역 식별 실패 → 시군구만 입력된 경우. org 없이 전국 검색 후 필터.
    return None, None, text


class LawApiError(Exception):
    pass


class LawGoKrAPI:
    """법제처 국가법령정보 공동활용 OPEN API 래퍼 (XML 전용)."""

    def __init__(self, oc, timeout=20):
        self.oc = (oc or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "law-ordinance-network/1.0"

    # 내부 공통 호출 ------------------------------------------------------
    def _get_xml(self, endpoint, params):
        params = {"OC": self.oc, "type": "XML", **params}
        url = f"{API_BASE}/{endpoint}?{urlencode(params, encoding='utf-8')}"
        # 일시적 연결 끊김·요청제한(특히 공용 데모키)에 대비해 점증 대기로 재시도한다.
        content = None
        last_err = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=self.timeout)
                r.raise_for_status()
                content = r.content
                break
            except requests.RequestException as e:
                last_err = e
                time.sleep(0.7 * (attempt + 1))
        if content is None:
            raise LawApiError(
                "네트워크 오류(3회 재시도 실패):\n" + str(last_err) +
                "\n\n· 잠시 후 다시 시도해 보세요.\n"
                "· 공용 데모키는 호출이 제한될 수 있으니 본인 인증키(OC) 사용을 권장합니다.")
        # 인증 실패 시 법제처는 HTML 안내문을 반환 → XML 파싱 실패
        try:
            return ET.fromstring(content)
        except ET.ParseError:
            head = content[:200].decode("utf-8", "replace")
            if "OC" in head or "인증" in head or "<html" in head.lower():
                raise LawApiError(
                    "API 인증키(OC)가 올바르지 않거나 아직 승인되지 않았습니다.\n"
                    "법제처 OPEN API 활용신청 후 승인받은 인증키(OC)인지 확인하세요.\n"
                    "(승인 전이거나 값이 틀리면 동작하지 않습니다. open.law.go.kr 마이페이지에서 확인)"
                )
            raise LawApiError("응답을 해석할 수 없습니다. 잠시 후 다시 시도하세요.")

    # 법령 검색 ----------------------------------------------------------
    def search_law(self, name, display=5):
        root = self._get_xml("lawSearch.do",
                             {"target": "law", "query": name, "display": display})
        results = []
        for law in root.findall("law"):
            results.append({
                "name": _ftext(law, "법령명한글", "법령명"),
                "law_id": _ftext(law, "법령ID"),
                "mst": _ftext(law, "법령일련번호", "법령MST"),
                "promulgation": _ftext(law, "공포일자"),
                "kind": _ftext(law, "법령구분명"),
                "ministry": _ftext(law, "소관부처명"),
            })
        return results

    # 법령 메타(이전 법령명·약칭) 조회 -----------------------------------
    def get_law_meta(self, mst):
        """법령 본문 기본정보에서 정식명·이전법령명(제명변경 전 이름)·약칭 등을 추출."""
        root = self._get_xml("lawService.do", {"target": "law", "MST": mst})
        base = root.find("기본정보")
        src = base if base is not None else root  # 기본정보가 루트 직계인 경우 대비
        return {
            "name": _ftext(src, "법령명_한글", "법령명한글"),
            "prev_name": _ftext(src, "이전법령명"),
            "alias": _ftext(src, "법령명약칭"),
            "changed": _ftext(src, "제명변경여부"),
            "ministry": _ftext(src, "소관부처", "소관부처명"),
            "law_id": _ftext(src, "법령ID"),
        }

    # 법령 조문 목록 (번호·제목·이동이력) — 끊어진 참조(7-5-4) 추적용 --------
    def get_law_articles(self, mst):
        """현행 법령의 조문 목록을 돌려준다: [{no,title,moved_from,moved_to,changed,content}]."""
        root = self._get_xml("lawService.do", {"target": "law", "MST": mst})
        arts = []
        for jo in root.iter("조문단위"):
            if (_ftext(jo, "조문여부") or "") != "조문":
                continue  # 장·절 제목(전문) 행 제외
            # 조문내용은 제목줄만 담길 때가 많으므로 항·호·목 내용까지 합쳐 전체 본문을 만든다.
            parts = [_ftext(jo, "조문내용")]
            for sub in jo.iter():
                if sub.tag in ("항내용", "호내용", "목내용") and sub.text:
                    parts.append(sub.text.strip())
            arts.append({
                "no": _ftext(jo, "조문번호"),
                "title": _ftext(jo, "조문제목"),
                "moved_from": _ftext(jo, "조문이동이전"),
                "moved_to": _ftext(jo, "조문이동이후"),
                "changed": _ftext(jo, "조문변경여부"),
                "content": " ".join(p for p in parts if p),
            })
        return arts

    # 자치법규(조례·규칙) 검색 -------------------------------------------
    def search_ordinance(self, query, org=None, search=2, display=100, page=1):
        """search=2: 본문 인용 검색(그 법률을 인용하는 조례) / search=1: 자치법규명 검색."""
        params = {"target": "ordin", "query": query,
                  "search": search, "display": display, "page": page}
        if org:
            params["org"] = org
        root = self._get_xml("lawSearch.do", params)
        total = int(_ftext(root, "totalCnt") or "0")
        items = []
        for law in root.findall("law"):
            items.append({
                "mst": _ftext(law, "자치법규일련번호"),
                "ord_id": _ftext(law, "자치법규ID"),
                "name": _ftext(law, "자치법규명"),
                "gov": _ftext(law, "지자체기관명"),
                "kind": _ftext(law, "자치법규종류"),
                "promulgation": _ftext(law, "공포일자"),
                "enforce": _ftext(law, "시행일자"),
                "revision": _ftext(law, "제개정구분명"),
                "field": _ftext(law, "자치법규분야명"),
            })
        return total, items

    def search_ordinance_all(self, query, org=None, search=2, max_count=200,
                             progress=None):
        """페이지를 넘기며 max_count 까지 모은다."""
        collected = []
        page = 1
        total = None
        while len(collected) < max_count:
            t, items = self.search_ordinance(query, org=org, search=search,
                                             display=100, page=page)
            if total is None:
                total = t
            if not items:
                break
            collected.extend(items)
            if progress:
                progress(min(len(collected), max_count), min(total, max_count))
            if len(items) < 100:
                break
            page += 1
        return total or 0, collected[:max_count]

    # 자치법규 본문 조회 -------------------------------------------------
    def get_ordinance_articles(self, mst):
        """본문 조문 목록 [(조제목, 조내용), ...] 과 기본정보를 돌려준다."""
        root = self._get_xml("lawService.do", {"target": "ordin", "MST": mst})
        info = root.find("자치법규기본정보")
        meta = {}
        if info is not None:
            meta = {
                "name": _ftext(info, "자치법규명"),
                "gov": _ftext(info, "지자체기관명"),
                "dept": _ftext(info, "담당부서명"),
                "enforce": _ftext(info, "시행일자"),
            }
        articles = []
        for jo in root.iter("조"):
            if (_ftext(jo, "조문여부") or "").strip() == "N":
                continue  # 장·절 제목 행 제외
            title = _ftext(jo, "조제목")
            content = _ftext(jo, "조내용")
            if content:
                articles.append((title, content))
        return meta, articles


def _ftext(elem, *tags):
    """여러 후보 태그 중 먼저 찾히는 텍스트 반환(공백 정리)."""
    if elem is None:
        return ""
    for t in tags:
        node = elem.find(t)
        if node is not None and node.text:
            return node.text.strip()
    return ""


# ──────────────────────────────────────────────────────────────────────────
# 연결(인용) 분석 — CoVe: 조례 원문 제1조에 「법률명」이 실제 있는지 대조
# ──────────────────────────────────────────────────────────────────────────
LINK_GROUND = "근거(제1조 위임)"   # 가장 강한 연결: 그 법의 위임으로 제정된 조례
LINK_CITE = "인용(본문)"           # 본문 조문에서 그 법을 인용
LINK_WEAK = "관련(간접)"           # 본문 검증했으나 「법률명」 직접 인용은 없음(검색만 매칭)
LINK_NONE = "미검증"               # 본문을 확인하지 않음


def collect_law_terms(name, prev_name, alias, extra_text=""):
    """현행 법령명 + 이전법령명 + 약칭 + 사용자 추가어를 중복 없이 검색어 리스트로.
    리스트의 0번이 현행명(가장 우선)."""
    terms = []
    for t in (name, prev_name, alias):
        t = (t or "").strip()
        if t and t not in terms:
            terms.append(t)
    for e in re.split(r"[,;/\n]", extra_text or ""):
        e = e.strip()
        if e and e not in terms:
            terms.append(e)
    return terms


def analyze_link(articles, law_terms):
    """본문 조문에서 law_terms(현행명·이전명·약칭 등) 중 하나의 인용을 찾아
    (연결강도, 인용조문, 인용문장요약, 인용된_명칭) 반환.
    law_terms[0]=현행명이 우선이므로, 현행명이 있으면 현행으로 잡힌다."""
    # 제1조(목적) 우선 확인 -------------------------------------------
    for title, content in articles:
        is_first = content.startswith("제1조") or ("목적" in (title or ""))
        if is_first:
            for t in law_terms:
                if t and t in content:
                    return LINK_GROUND, "제1조(목적)", _snippet(content, t), t
    # 그 밖의 조문에서 인용 -------------------------------------------
    for title, content in articles:
        for t in law_terms:
            if t and t in content:
                m = re.match(r"(제\d+조(?:의\d+)?)", content)
                jo = m.group(1) if m else (title or "본문")
                return LINK_CITE, jo, _snippet(content, t), t
    return LINK_WEAK, "-", "", ""


def _snippet(text, keyword, span=35):
    i = text.find(keyword)
    if i < 0:
        return text[:span * 2].replace("\n", " ")
    s = max(0, i - span)
    e = min(len(text), i + len(keyword) + span)
    return ("…" if s > 0 else "") + text[s:e].replace("\n", " ") + ("…" if e < len(text) else "")


# ──────────────────────────────────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────────────────────────────────
KIND_COLOR = {LINK_GROUND: "#2e7d32", LINK_CITE: "#ef6c00",
              LINK_WEAK: "#8e24aa", LINK_NONE: "#9e9e9e"}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("법률–조례 연결 확인기 · 법률네트워크")
        self.geometry("1240x780")
        self.minsize(1040, 640)

        self.api = None
        self.rows = []          # 현재 표에 표시 중인 조례 데이터
        self.law_info = None    # 확인된 법령 정보
        self.law_terms = []     # 검색에 쓴 별칭들 [현행, 이전, 약칭, 추가…]
        self.current_name = ""  # 확정된 현행 법령명
        self.prev_name = ""     # 이전 법령명(제명변경 전)
        self._build_ui()

    # UI 구성 ------------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Accent.TButton", font=("Malgun Gothic", 10, "bold"))

        # ── 입력 영역 ────────────────────────────────────────────────
        top = ttk.LabelFrame(self, text="조회 조건")
        top.pack(fill="x", padx=10, pady=(10, 6))

        # row 0 — 법률명(+법령 확인) / 지자체(광역·시군구 콤보)
        ttk.Label(top, text="법률명").grid(row=0, column=0, padx=(10, 4), pady=(10, 4), sticky="e")
        self.var_law = tk.StringVar(value="")   # 기본값 비움(예시값 오조회 방지)
        self.ent_law = ttk.Entry(top, textvariable=self.var_law, width=24)
        self.ent_law.grid(row=0, column=1, pady=(10, 4), sticky="w")
        self.var_law.trace_add("write", lambda *a: self.var_lawinfo.set(""))  # 법률명 바뀌면 확인표시 초기화
        ttk.Button(top, text="🔎 법령 확인", command=self.on_check_law).grid(
            row=0, column=2, padx=(4, 0), pady=(10, 4))

        ttk.Label(top, text="지자체").grid(row=0, column=3, padx=(18, 4), pady=(10, 4), sticky="e")
        self.var_sido = tk.StringVar(value="서울특별시")
        self.cmb_sido = ttk.Combobox(top, textvariable=self.var_sido, width=14, state="readonly",
                                     values=list(SIDO_CODE))
        self.cmb_sido.grid(row=0, column=4, pady=(10, 4), sticky="w")
        self.cmb_sido.bind("<<ComboboxSelected>>", self.on_sido_change)
        self.var_sigungu = tk.StringVar(value=SIGUNGU_ALL)
        self.cmb_sigungu = ttk.Combobox(top, textvariable=self.var_sigungu, width=12)  # 편집 가능
        self.cmb_sigungu.grid(row=0, column=5, padx=(4, 0), pady=(10, 4), sticky="w")

        # row 1 — API 인증키 / 추가 검색어(옛 이름·약칭)
        ttk.Label(top, text="API 인증키(OC)").grid(row=1, column=0, padx=(10, 4), pady=4, sticky="e")
        self.var_oc = tk.StringVar(value=DEFAULT_OC)
        ttk.Entry(top, textvariable=self.var_oc, width=24, show="•").grid(row=1, column=1, pady=4, sticky="w")
        ttk.Label(top, text="추가 검색어").grid(row=1, column=3, padx=(18, 4), pady=4, sticky="e")
        self.var_extra = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_extra, width=28).grid(
            row=1, column=4, columnspan=2, pady=4, sticky="w")

        # row 2 — 법령 확인 결과(이전 명칭 포함)
        self.var_lawinfo = tk.StringVar(
            value="ⓘ [🔎 법령 확인]으로 정식명칭을 고르면 옛 이름(제명변경 전)도 자동으로 함께 검색합니다. "
                  "‘추가 검색어’ 칸에 옛 이름·약칭을 직접(쉼표 구분) 넣어도 됩니다.")
        ttk.Label(top, textvariable=self.var_lawinfo, foreground="#1565c0").grid(
            row=2, column=0, columnspan=6, sticky="w", padx=(12, 0), pady=(0, 2))

        # row 3 — 옵션 + 실행 버튼
        opt = ttk.Frame(top)
        opt.grid(row=3, column=0, columnspan=6, sticky="we", padx=10, pady=(2, 8))
        self.var_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="본문 검증(제1조 인용 확인 · 권장)", variable=self.var_verify).pack(side="left")
        self.var_ord_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="조례만(규칙 제외)", variable=self.var_ord_only).pack(side="left", padx=(14, 0))
        ttk.Label(opt, text="최대 표시").pack(side="left", padx=(16, 4))
        self.var_max = tk.IntVar(value=200)
        ttk.Spinbox(opt, from_=20, to=1000, increment=20, width=6, textvariable=self.var_max).pack(side="left")
        ttk.Label(opt, text="건  · 검증 상위").pack(side="left", padx=(8, 4))
        self.var_vmax = tk.IntVar(value=40)
        ttk.Spinbox(opt, from_=5, to=200, increment=5, width=6, textvariable=self.var_vmax).pack(side="left")
        ttk.Label(opt, text="건").pack(side="left", padx=(4, 0))
        ttk.Button(opt, text="도움말", command=self.show_help).pack(side="left", padx=(20, 0))
        ttk.Button(opt, text="API키 발급 안내", command=self.open_apikey_page).pack(side="left", padx=(6, 0))
        self.btn_run = ttk.Button(opt, text="🔍  연결 조회", style="Accent.TButton", command=self.on_search)
        self.btn_run.pack(side="right")

        for c in range(6):
            top.columnconfigure(c, weight=0)

        # ── 본문: 좌(표) / 우(그래프) ────────────────────────────────
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        body.add(left, weight=3)
        cols = ("name", "gov", "kind", "link", "cited", "jo", "enforce")
        heads = {"name": "자치법규명", "gov": "지자체", "kind": "종류",
                 "link": "연결강도", "cited": "인용명칭", "jo": "인용조문", "enforce": "시행일"}
        widths = {"name": 268, "gov": 124, "kind": 52, "link": 116, "cited": 140, "jo": 78, "enforce": 80}
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.tree.heading(c, text=heads[c], command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.tag_configure("ground", foreground=KIND_COLOR[LINK_GROUND])
        self.tree.tag_configure("cite", foreground=KIND_COLOR[LINK_CITE])
        self.tree.tag_configure("weak", foreground=KIND_COLOR[LINK_WEAK])
        self.tree.tag_configure("none", foreground=KIND_COLOR[LINK_NONE])
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self.on_open_detail)

        right = ttk.LabelFrame(body, text="법령 인용 네트워크  (조례 ──인용──▶ 법률)")
        body.add(right, weight=4)
        self.fig = Figure(figsize=(5.2, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.axis("off")
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._draw_placeholder()

        # ── 하단 상태바 ──────────────────────────────────────────────
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.var_status = tk.StringVar(value="법률명·지자체·API키를 입력하고 [연결 조회]를 누르세요.")
        ttk.Label(bottom, textvariable=self.var_status, anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, length=180, mode="determinate")
        self.progress.pack(side="left", padx=8)
        self.btn_csv = ttk.Button(bottom, text="CSV 저장", command=self.save_csv, state="disabled")
        self.btn_csv.pack(side="left")
        self.btn_amend = ttk.Button(bottom, text="✏️ 개정안 제안", command=self.on_amendment, state="disabled")
        self.btn_amend.pack(side="left", padx=(6, 0))

        self.on_sido_change()  # 초기 시군구 목록 채우기

    # 지자체 확인 — 광역 선택 시 시군구 목록 갱신 -------------------------
    def on_sido_change(self, _event=None):
        sido = self.var_sido.get()
        gus = SIGUNGU.get(sido, [])
        self.cmb_sigungu.config(values=[SIGUNGU_ALL] + gus)
        self.var_sigungu.set(SIGUNGU_ALL)

    # 법령 확인 — 입력한 법률명의 실제 법령 후보를 조회해 선택 -------------
    def on_check_law(self):
        kw = self.var_law.get().strip()
        oc = self.var_oc.get().strip()
        if not kw:
            messagebox.showwarning("입력 확인", "먼저 법률명을 입력하세요.")
            return
        if not oc:
            messagebox.showwarning("API키 필요", "법령 확인에도 법제처 API 인증키(OC)가 필요합니다.")
            return
        api = LawGoKrAPI(oc)
        self.var_lawinfo.set("법령 확인 중…")

        def work():
            try:
                laws = api.search_law(kw, display=20)
            except LawApiError as e:
                msg = str(e)
                self._ui(lambda: (self.var_lawinfo.set(""), messagebox.showerror("오류", msg)))
                return
            except Exception as e:
                msg = str(e)
                self._ui(lambda: (self.var_lawinfo.set(""), messagebox.showerror("오류", "법령 확인 실패: " + msg)))
                return
            self._ui(lambda: self._show_law_dialog(kw, laws, api))
        threading.Thread(target=work, daemon=True).start()

    def _show_law_dialog(self, kw, laws, api):
        self.var_lawinfo.set("")
        if not laws:
            messagebox.showinfo("법령 확인",
                                f"‘{kw}’ 에 해당하는 법령을 찾지 못했습니다.\n"
                                "띄어쓰기를 줄이거나 정식 명칭 일부로 다시 검색해 보세요.")
            return
        dlg = tk.Toplevel(self)
        dlg.title(f"법령 확인 — ‘{kw}’ 검색 결과 {len(laws)}건")
        dlg.geometry("660x380")
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text="정확한 법령을 고르세요. 더블클릭 또는 [이 법령 선택].",
                  padding=8).pack(anchor="w")
        cols = ("name", "kind", "ministry", "prom")
        heads = {"name": "법령명", "kind": "구분", "ministry": "소관부처", "prom": "공포일"}
        widths = {"name": 300, "kind": 90, "ministry": 150, "prom": 90}
        tv = ttk.Treeview(dlg, columns=cols, show="headings", height=11, selectmode="browse")
        for c in cols:
            tv.heading(c, text=heads[c])
            tv.column(c, width=widths[c], anchor="w")
        for x in laws:
            tv.insert("", "end", values=(x["name"], x["kind"] or "-", x["ministry"] or "-", x["promulgation"] or "-"))
        tv.pack(fill="both", expand=True, padx=8)
        # 기본 선택: 첫 '법률'
        for i, x in enumerate(laws):
            if x["kind"] == "법률":
                tv.selection_set(tv.get_children()[i])
                tv.see(tv.get_children()[i])
                break

        def choose(_e=None):
            sel = tv.selection()
            if not sel:
                return
            x = laws[tv.index(sel[0])]
            self.var_law.set(x["name"])
            self.law_info = x
            base = f"✓ 확인됨: {x['name']}  ({x['kind'] or '-'} · {x['ministry'] or '-'})"
            self.var_lawinfo.set(base + "   · 옛 이름 확인 중…")
            dlg.destroy()
            mst = x.get("mst")
            if not mst:
                self.var_lawinfo.set(base)
                return

            def fetch_prev():   # 이전 법령명(제명변경 전)을 조회해 표시
                try:
                    meta = api.get_law_meta(mst)
                    prev = meta.get("prev_name", "")
                    if prev:
                        disp = base + f"    ⚠ 옛 이름: 「{prev}」 — 조회 시 함께 검색됩니다"
                    else:
                        disp = base + "   (제명변경 이력 없음)"
                    self._ui(lambda: self.var_lawinfo.set(disp))
                except Exception:
                    self._ui(lambda: self.var_lawinfo.set(base))
            threading.Thread(target=fetch_prev, daemon=True).start()
        tv.bind("<Double-1>", choose)
        bf = ttk.Frame(dlg)
        bf.pack(fill="x", pady=8, padx=8)
        ttk.Button(bf, text="이 법령 선택", style="Accent.TButton", command=choose).pack(side="right", padx=(8, 0))
        ttk.Button(bf, text="취소", command=dlg.destroy).pack(side="right")
        dlg.bind("<Return>", choose)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # 조회 실행 ----------------------------------------------------------
    def on_search(self):
        law = self.var_law.get().strip()
        sido = self.var_sido.get().strip()
        sigungu = self.var_sigungu.get().strip()
        if sigungu == SIGUNGU_ALL:
            sigungu = ""
        oc = self.var_oc.get().strip()
        if not law:
            messagebox.showwarning("입력 확인", "법률명을 입력하세요.")
            return
        if not sido:
            messagebox.showwarning("입력 확인", "지자체(광역)를 선택하세요.")
            return
        if not oc:
            messagebox.showwarning("API키 필요",
                                   "법제처 OPEN API 인증키(OC)를 입력하세요.\n"
                                   "[API키 발급 안내] 버튼을 참고하세요.")
            return
        org = SIDO_CODE.get(sido)
        self.api = LawGoKrAPI(oc)
        self.btn_run.config(state="disabled")
        self.btn_csv.config(state="disabled")
        self.btn_amend.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        self.progress.config(value=0)
        # tkinter 변수는 메인 스레드에서만 안전하게 읽을 수 있으므로 여기서 모두 읽어 넘긴다.
        opts = {
            "verify": self.var_verify.get(),
            "vmax": int(self.var_vmax.get()),
            "max": int(self.var_max.get()),
            "ord_only": self.var_ord_only.get(),
            "extra": self.var_extra.get().strip(),
        }
        threading.Thread(target=self._worker, args=(law, sido, org, sigungu, opts), daemon=True).start()

    def _worker(self, law, sido, org, sigungu, opts):
        try:
            self._ui(lambda: self.var_status.set(
                f"‘{law}’ 법령 확인 중…  (지역: {sido}{' ' + sigungu if sigungu else ''})"))

            # 1) 정식명 + 이전법령명(제명변경 전)·약칭 확보 ----------------
            law_name = law
            prev_name = alias = ""
            try:
                laws = self.api.search_law(law)
                pick = next((x for x in laws if x["kind"] == "법률"), laws[0] if laws else None)
                if pick:
                    law_name = pick["name"] or law
                    self.law_info = pick
                    if pick.get("mst"):
                        try:
                            meta = self.api.get_law_meta(pick["mst"])
                            prev_name = meta.get("prev_name", "")
                            alias = meta.get("alias", "")
                        except Exception:
                            pass
            except LawApiError:
                raise
            except Exception:
                self.law_info = None

            terms = collect_law_terms(law_name, prev_name, alias, opts.get("extra", ""))
            self.law_terms = terms
            self.current_name = law_name
            self.prev_name = prev_name

            # 2) 검색어(현행+이전명+약칭+추가어)별 본문검색 → MST로 합집합 ----
            alias_note = ("  (+별칭: " + ", ".join(terms[1:]) + ")") if len(terms) > 1 else ""
            self._ui(lambda: self.var_status.set(f"‘{law_name}’{alias_note} 인용 자치법규 검색 중…"))
            display_cap = opts["max"]
            collect_cap = max(display_cap, 800) if sigungu else display_cap
            merged = {}
            total_cur = 0
            for ti, term in enumerate(terms):
                def prog(cur, tot, _t=term):
                    self._ui(lambda: self._set_progress(cur, tot, f"‘{_t}’ 검색 {cur}/{tot}"))
                t_total, t_items = self.api.search_ordinance_all(
                    term, org=org, search=2, max_count=collect_cap, progress=prog)
                if ti == 0:
                    total_cur = t_total
                for it in t_items:
                    merged.setdefault(it["mst"], it)
            items = list(merged.values())

            # 시군구 / 종류 필터
            if sigungu:
                items = [x for x in items if sigungu in (x["gov"] or "")]
            if opts["ord_only"]:
                items = [x for x in items if x["kind"] == "조례"]
            items = items[:display_cap]   # 표시 상한

            if not items:
                self._ui(lambda: self._finish_empty(law_name, sido, sigungu, total_cur))
                return

            # 3) 본문 검증(CoVe) — 어떤 명칭(현행/구명)으로 인용했는지까지 -----
            verify = opts["verify"]
            vmax = opts["vmax"]
            for idx, it in enumerate(items):
                if verify and idx < vmax:
                    self._ui(lambda i=idx: self._set_progress(i + 1, min(len(items), vmax),
                                                              f"본문 검증 {i + 1}/{min(len(items), vmax)}"))
                    try:
                        _, arts = self.api.get_ordinance_articles(it["mst"])
                        strength, jo, snip, cited = analyze_link(arts, terms)
                    except Exception:
                        strength, jo, snip, cited = LINK_NONE, "-", "", ""
                    it["link"], it["jo"], it["snippet"], it["cited"] = strength, jo, snip, cited
                else:
                    it["link"], it["jo"], it["snippet"], it["cited"] = LINK_NONE, "-", "", ""

            # 연결강도 순 정렬 (근거 > 인용 > 관련 > 미검증), 그 안에서 지자체명
            order = {LINK_GROUND: 0, LINK_CITE: 1, LINK_WEAK: 2, LINK_NONE: 3}
            items.sort(key=lambda x: (order.get(x["link"], 3), x["gov"] or ""))
            self.rows = items
            self._ui(lambda: self._render(law_name, sido, sigungu, total_cur))
        except LawApiError as e:
            # except 블록을 벗어나면 e 가 사라지므로 미리 문자열로 잡아 클로저에 넘긴다.
            msg = str(e)
            self._ui(lambda m=msg: self._error(m))
        except Exception as e:
            import traceback
            msg = "예기치 못한 오류: " + str(e)
            detail = traceback.format_exc()
            print(detail)  # 콘솔에도 남겨 디버깅 가능하게
            self._ui(lambda m=msg: self._error(m))

    # 결과 렌더링 --------------------------------------------------------
    def _cited_disp(self, it):
        """인용명칭 컬럼 표시값. 현행명이면 '현행', 옛 이름/약칭이면 '⚠ 이름'."""
        c = it.get("cited", "")
        if not c:
            return "-"
        if c == self.current_name:
            return "현행"
        return "⚠ " + c

    def _insert_row(self, it, tagmap):
        self.tree.insert("", "end", values=(
            it["name"], it["gov"], it["kind"], it["link"], self._cited_disp(it),
            it["jo"], it["enforce"]),
            tags=(tagmap.get(it["link"], "none"),))

    def _render(self, law_name, sido, sigungu, total):
        tagmap = {LINK_GROUND: "ground", LINK_CITE: "cite", LINK_WEAK: "weak", LINK_NONE: "none"}
        for it in self.rows:
            self._insert_row(it, tagmap)
        n_ground = sum(1 for x in self.rows if x["link"] == LINK_GROUND)
        n_cite = sum(1 for x in self.rows if x["link"] == LINK_CITE)
        n_weak = sum(1 for x in self.rows if x["link"] == LINK_WEAK)
        n_none = sum(1 for x in self.rows if x["link"] == LINK_NONE)
        n_old = sum(1 for x in self.rows
                    if x.get("cited") and x.get("cited") != self.current_name)
        scope = (sido or "전국") + (" " + sigungu if sigungu else "")
        old_note = f"   ⚠ 옛 명칭 인용 {n_old}건(정비 대상)" if n_old else ""
        alias_flag = "   ※이전명 포함검색" if len(self.law_terms) > 1 else ""
        self.var_status.set(
            f"‘{law_name}’ ↔ {scope}  |  표시 {len(self.rows)}건 "
            f"(근거 {n_ground} · 인용 {n_cite} · 관련 {n_weak} · 미검증 {n_none})"
            f"{old_note}{alias_flag}")
        self.progress.config(value=0)
        self.btn_run.config(state="normal")
        self.btn_csv.config(state="normal")
        self.btn_amend.config(state="normal")
        self._draw_network(law_name)

    def _finish_empty(self, law_name, sido, sigungu, total):
        self.progress.config(value=0)
        self.btn_run.config(state="normal")
        scope = (sido or "전국") + (" " + sigungu if sigungu else "")
        self.var_status.set(f"‘{law_name}’ ↔ {scope} : 연결된 조례를 찾지 못했습니다. (전국 인용 {total}건)")
        self._draw_placeholder(f"‘{law_name}’ 을(를) 인용하는\n{scope} 자치법규가 없습니다.")
        messagebox.showinfo("결과 없음",
                            f"‘{law_name}’ 을(를) 인용하는 {scope} 자치법규를 찾지 못했습니다.\n\n"
                            "· 법률명을 정식 명칭으로(예: ‘주차장법’) 입력했는지\n"
                            "· 지자체명이 맞는지\n 확인해 보세요.")

    def _error(self, msg):
        self.progress.config(value=0)
        self.btn_run.config(state="normal")
        self.var_status.set("오류: " + msg.splitlines()[0])
        messagebox.showerror("오류", msg)

    # 네트워크 그래프 ----------------------------------------------------
    def _draw_placeholder(self, text="여기에 법령 인용 네트워크가 표시됩니다."):
        self.ax.clear()
        self.ax.axis("off")
        self.ax.text(0.5, 0.5, text, ha="center", va="center",
                     fontsize=12, color="#888")
        self.canvas.draw()

    def _draw_network(self, law_name):
        self.ax.clear()
        self.ax.axis("off")
        G = nx.DiGraph()
        center = law_name
        G.add_node(center, kind="law")

        MAX_NODES = 36
        shown = self.rows[:MAX_NODES]
        for it in shown:
            label = f"{_short_gov(it['gov'])}\n{it['kind']}"
            # 동일 라벨 충돌 방지
            key = label + "·" + it["mst"]
            G.add_node(key, kind=it["link"], label=label)
            G.add_edge(key, center, kind=it["link"])

        # 중앙(법률) 고정, 조례는 원형 배치
        pos = {center: (0, 0)}
        import math
        others = [n for n in G.nodes if n != center]
        for i, n in enumerate(others):
            ang = 2 * math.pi * i / max(1, len(others))
            pos[n] = (math.cos(ang), math.sin(ang))

        # 엣지 색 = 연결강도
        ecolors = [KIND_COLOR.get(G.edges[e]["kind"], "#bbb") for e in G.edges]
        nx.draw_networkx_edges(G, pos, ax=self.ax, edge_color=ecolors,
                               arrows=True, arrowsize=11, width=1.4,
                               connectionstyle="arc3,rad=0.04", alpha=0.7)

        # 노드
        node_colors, node_sizes = [], []
        for n in G.nodes:
            if n == center:
                node_colors.append("#c62828"); node_sizes.append(2600)
            else:
                node_colors.append(KIND_COLOR.get(G.nodes[n]["kind"], "#9e9e9e"))
                node_sizes.append(900)
        nx.draw_networkx_nodes(G, pos, ax=self.ax, node_color=node_colors,
                               node_size=node_sizes, edgecolors="white", linewidths=1.2)

        labels = {center: center}
        for n in others:
            labels[n] = G.nodes[n].get("label", "")
        nx.draw_networkx_labels(G, pos, labels=labels, ax=self.ax, font_size=7,
                                font_family=rcParams["font.family"])

        # 범례
        from matplotlib.lines import Line2D
        legend = [
            Line2D([0], [0], marker="o", color="w", label="법률(중심)", markerfacecolor="#c62828", markersize=10),
            Line2D([0], [0], marker="o", color="w", label=LINK_GROUND, markerfacecolor=KIND_COLOR[LINK_GROUND], markersize=9),
            Line2D([0], [0], marker="o", color="w", label=LINK_CITE, markerfacecolor=KIND_COLOR[LINK_CITE], markersize=9),
            Line2D([0], [0], marker="o", color="w", label=LINK_WEAK, markerfacecolor=KIND_COLOR[LINK_WEAK], markersize=9),
            Line2D([0], [0], marker="o", color="w", label=LINK_NONE, markerfacecolor=KIND_COLOR[LINK_NONE], markersize=9),
        ]
        self.ax.legend(handles=legend, loc="upper right", fontsize=7, framealpha=0.9)
        extra = "" if len(self.rows) <= MAX_NODES else f"  (상위 {MAX_NODES}건만 표시)"
        self.ax.set_title(f"{center} ← 인용 자치법규 {len(shown)}건{extra}", fontsize=10)
        self.fig.tight_layout()
        self.canvas.draw()

    # 행 더블클릭 → 상세 페이지 -----------------------------------------
    def on_open_detail(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self.rows):
            return
        it = self.rows[idx]
        url = DETAIL_URL.format(oc=self.api.oc if self.api else "test", mst=it["mst"])
        webbrowser.open(url)

    # 정렬 ---------------------------------------------------------------
    def _sort_by(self, col):
        if not self.rows:
            return
        keymap = {"name": "name", "gov": "gov", "kind": "kind",
                  "link": "link", "cited": "cited", "jo": "jo", "enforce": "enforce"}
        k = keymap.get(col, "gov")
        rev = getattr(self, "_sort_rev_" + col, False)
        self.rows.sort(key=lambda x: (x.get(k) or ""), reverse=rev)
        setattr(self, "_sort_rev_" + col, not rev)
        self.tree.delete(*self.tree.get_children())
        tagmap = {LINK_GROUND: "ground", LINK_CITE: "cite", LINK_WEAK: "weak", LINK_NONE: "none"}
        for it in self.rows:
            self._insert_row(it, tagmap)

    # CSV 저장 -----------------------------------------------------------
    def save_csv(self):
        if not self.rows:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV 파일", "*.csv")],
            initialfile="법률조례_연결.csv")
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["자치법규명", "지자체", "종류", "연결강도", "인용명칭", "인용조문",
                        "인용문장", "시행일", "공포일", "제개정", "MST"])
            for it in self.rows:
                w.writerow([it["name"], it["gov"], it["kind"], it["link"],
                            it.get("cited", ""), it["jo"],
                            it.get("snippet", ""), it["enforce"], it["promulgation"],
                            it["revision"], it["mst"]])
        self.var_status.set(f"CSV 저장 완료: {path}")
        messagebox.showinfo("저장 완료", f"{len(self.rows)}건을 저장했습니다.\n{path}")

    # 개정안 제안 (OpenAI) — 옛 명칭 인용 조례를 어떻게 고칠지 신구대조표 -----
    def on_amendment(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("개정안 제안", "표에서 조례를 한 건 선택하세요.\n"
                                "(⚠ 옛 명칭을 인용하는 정비 대상 조례에 대해 제안됩니다.)")
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self.rows):
            return
        it = self.rows[idx]
        cited = it.get("cited", "")
        if not cited:
            messagebox.showinfo("개정안 불필요",
                "이 조례는 본문 미검증이거나 법령명 인용이 확인되지 않았습니다.\n"
                "개정안은 ⚠ 옛 명칭을 인용하는 조례에 대해 제안됩니다.")
            return
        if cited == self.current_name:
            messagebox.showinfo("개정안 불필요",
                f"이 조례는 이미 현행 명칭(「{self.current_name}」)을 인용하고 있어\n"
                "명칭 정비가 필요하지 않습니다.")
            return
        if not DEFAULT_OPENAI:
            messagebox.showwarning("OpenAI 키 필요",
                "개정안 생성에는 OpenAI API 키가 필요합니다.\n"
                ".env 파일에 OPENAI_API_KEY 를 설정하세요(.env.example 참고).")
            return
        old_name, new_name = cited, self.current_name
        self.btn_amend.config(state="disabled")
        self.var_status.set(f"‘{it['name']}’ 개정안 생성 중… (OpenAI {OPENAI_MODEL})")

        def work():
            try:
                _, arts = self.api.get_ordinance_articles(it["mst"])
                hit = [(t, c) for (t, c) in arts if old_name in c]
                if not hit:
                    self._ui(lambda: (self.var_status.set("개정안: 옛 명칭이 본문에 없습니다"),
                                      self.btn_amend.config(state="normal"),
                                      messagebox.showinfo("개정안", "본문에서 옛 명칭을 찾지 못했습니다.")))
                    return
                # 현행법 조문 목록 조회 → '인용한 제N조'가 유효한지(끊어진 참조) 점검에 사용
                law_arts = None
                try:
                    mst_law = (self.law_info or {}).get("mst")
                    if mst_law:
                        law_arts = self.api.get_law_articles(mst_law)
                except Exception:
                    law_arts = None
                result = generate_amendment(DEFAULT_OPENAI, it["name"], it["gov"],
                                            old_name, new_name, hit, law_articles=law_arts)
                self._ui(lambda: self._show_amendment(it, old_name, new_name, result))
            except Exception as e:
                msg = str(e)
                self._ui(lambda m=msg: (self.var_status.set("개정안 생성 실패"),
                                        self.btn_amend.config(state="normal"),
                                        messagebox.showerror("오류", "개정안 생성 실패:\n" + m)))
        threading.Thread(target=work, daemon=True).start()

    def _show_amendment(self, it, old_name, new_name, text):
        self.var_status.set(f"‘{it['name']}’ 개정안 생성 완료")
        self.btn_amend.config(state="normal")
        dlg = tk.Toplevel(self)
        dlg.title(f"개정안 — {it['name']}")
        dlg.geometry("820x600")
        dlg.transient(self)
        ttk.Label(dlg, text=f"{it['name']}  ({it['gov']})",
                  font=("Malgun Gothic", 12, "bold")).pack(anchor="w", padx=12, pady=(10, 2))
        ttk.Label(dlg, text=f"명칭 정비:  「{old_name}」  →  「{new_name}」",
                  foreground="#c62828").pack(anchor="w", padx=12, pady=(0, 8))
        frm = ttk.Frame(dlg)
        frm.pack(fill="both", expand=True, padx=12)
        txt = tk.Text(frm, wrap="word", font=("Malgun Gothic", 10))
        sb = ttk.Scrollbar(frm, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        bf = ttk.Frame(dlg)
        bf.pack(fill="x", pady=8, padx=12)
        ttk.Label(bf, text="⚠ AI가 만든 초안입니다. 반드시 원문·법제처와 대조 후 사용하세요.",
                  foreground="#8a3b00").pack(side="left")

        def save():
            p = filedialog.asksaveasfilename(
                defaultextension=".md",
                initialfile=f"개정안_{it['gov']}_{it['name']}.md".replace(" ", "_"),
                filetypes=[("Markdown", "*.md"), ("텍스트", "*.txt")])
            if not p:
                return
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"# 조례 명칭 정비 개정안\n\n"
                        f"- 조례: {it['name']} ({it['gov']})\n"
                        f"- 명칭 변경: 「{old_name}」 → 「{new_name}」\n\n{text}\n")
            messagebox.showinfo("저장 완료", f"개정안을 저장했습니다.\n{p}")
        ttk.Button(bf, text="저장(.md)", command=save).pack(side="right", padx=(8, 0))
        ttk.Button(bf, text="닫기", command=dlg.destroy).pack(side="right")

    # 도움말 -------------------------------------------------------------
    def show_help(self):
        msg = (
            "■ 무엇을 하나요?\n"
            "  입력한 ‘법률’을 상위법으로 인용·위임받은 ‘지자체’의 조례·규칙을 찾아\n"
            "  표와 네트워크 그래프로 보여줍니다.\n"
            "  (「공무원을 위한 AI 활용」 7-5장 ‘나를 인용하는 법 찾기’의 역방향 질의)\n\n"
            "■ 연결강도 (위가 강한 연결)\n"
            f"  · {LINK_GROUND} : 조례 제1조(목적)에 「법률명」 명시 — 그 법의 위임으로 제정된 조례\n"
            f"  · {LINK_CITE} : 본문 다른 조문에서 그 법을 인용\n"
            f"  · {LINK_WEAK} : 본문을 확인했으나 「법률명」 직접 인용은 없음(검색만 매칭, 간접 관련)\n"
            f"  · {LINK_NONE} : 본문을 확인하지 않음(‘검증 상위 N건’ 밖)\n\n"
            "■ 옛 법령명(제명변경) 자동 확장\n"
            "  법이 개정되며 이름이 바뀐 경우(예: 국가정보화 기본법 → 지능정보화 기본법),\n"
            "  옛 이름을 인용하는 조례까지 함께 찾아 ‘인용명칭’에 ⚠ 옛이름으로 표시합니다(정비 대상).\n"
            "  옛 이름은 법제처 정보에서 자동 인식하며, ‘추가 검색어’ 칸에 직접 넣을 수도 있습니다.\n\n"
            "■ 입력 팁\n"
            "  · 법률명은 정식 명칭으로 (예: 주차장법, 옥외광고물 등의 관리와 옥외광고산업 진흥에 관한 법률)\n"
            "  · 지자체는 ‘서울특별시’처럼 광역명, 또는 ‘서울특별시 강남구’/‘경기도 수원시’처럼 시군구까지\n"
            "  · 행을 더블클릭하면 국가법령정보센터 원문이 열립니다.\n\n"
            "■ 검증(CoVe)\n"
            "  검색 결과를 그대로 믿지 않고 조례 본문 원문을 다시 대조합니다.\n"
            "  건수가 많으면 ‘검증 상위 N건’만 본문을 확인하고 나머지는 ‘단순매칭’으로 둡니다."
        )
        messagebox.showinfo("도움말", msg)

    def open_apikey_page(self):
        messagebox.showinfo(
            "API 인증키(OC) 발급",
            "법제처 국가법령정보 공동활용 OPEN API 인증키(OC)는 무료이지만,\n"
            "활용신청과 담당자 승인을 거쳐야 발급·사용할 수 있습니다.\n\n"
            "1) open.law.go.kr 접속 → 회원가입 / 로그인\n"
            "2) [OPEN API → 활용신청] 에서 사용할 API와 활용사례를 등록해 신청\n"
            "3) 담당자 승인(보통 1~2일) 후 사용 가능\n"
            "4) 승인된 인증키(OC)는 로그인 후 ‘마이페이지 / 신청내역’ 에서 확인\n\n"
            "※ 인증키는 신청·승인으로 부여되는 값이라 임의로 만들 수 없습니다.\n"
            "확인을 누르면 신청 페이지를 엽니다.")
        webbrowser.open("https://open.law.go.kr/LSO/openApi/cuAskList.do")

    # 스레드 → UI 안전 호출 / 진행률 ------------------------------------
    def _ui(self, fn):
        self.after(0, fn)

    def _set_progress(self, cur, tot, text=None):
        self.progress.config(maximum=max(1, tot), value=cur)
        if text:
            self.var_status.set(text)


def _short_gov(gov):
    """그래프 라벨용으로 지자체명을 짧게 (광역 약칭 + 시군구)."""
    if not gov:
        return "?"
    parts = gov.split()
    if len(parts) >= 2:
        head = parts[0]
        for alias, full in SIDO_ALIAS.items():
            if full == head and len(alias) <= 2:
                head = alias
                break
        return head + " " + parts[-1]
    return gov


if __name__ == "__main__":
    App().mainloop()

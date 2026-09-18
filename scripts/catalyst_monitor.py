#!/usr/bin/env python3
"""
US Stock Catalyst Monitor
=========================
SEC EDGAR 실시간 공시(8-K / 6-K / 425 / SC TO-I)를 폴링해서
긍정적 촉매제(자사주매입, 소각, M&A, 파트너십, 임상, 탑라인, FDA 등)를
탐지하고 Telegram 알림 + docs/data/alerts.json 피드를 갱신한다.

사용법:
  python scripts/catalyst_monitor.py                      # 1회 실행
  python scripts/catalyst_monitor.py --loop --interval 30 # 30초 주기 루프
  python scripts/catalyst_monitor.py --dry-run            # 텔레그램 전송 없이 테스트

환경변수:
  TELEGRAM_BOT_TOKEN  (필수)
  TELEGRAM_CHAT_ID    (필수)
  SEC_USER_AGENT      (필수, 예: "hkpark hkpark@example.com")
                      SEC Fair Access 정책상 연락처 포함 UA 없으면 403.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent.parent
ALERTS_PATH = ROOT / "docs" / "data" / "alerts.json"
STATE_PATH = ROOT / "state" / "seen.json"
CONFIG_PATH = ROOT / "config.json"
CACHE_DIR = ROOT / ".cache"

KST = timezone(timedelta(hours=9))
ET_TZ = timezone(timedelta(hours=-4))  # EDGAR 타임스탬프는 오프셋 포함이라 참고용

MAX_ALERTS = 300          # 웹 피드에 보관할 최대 알림 수
MAX_SEEN = 2500           # 중복 방지용 accession 보관 수 (git 비대화 방지)
DOC_TEXT_LIMIT = 400_000  # 문서 1건당 파싱할 최대 바이트

# ---------------------------------------------------------------------------
# 촉매제 정의
# ---------------------------------------------------------------------------
# weight: 신뢰도 가중치, patterns: 대소문자 무시 정규식
CATALYSTS = [
    {
        "key": "buyback",
        "label": "자사주매입",
        "emoji": "💰",
        "weight": 14,
        "patterns": [
            r"(share|stock)\s+repurchase\s+(program|plan|authoriz\w+)",
            r"repurchase\s+authoriz\w+",
            r"authoriz\w+\s+(the\s+)?(repurchase|buyback)",
            r"accelerated\s+share\s+repurchase",
            r"\bbuyback\s+program\b",
            r"board\s+of\s+directors\s+(has\s+)?approved\s+.{0,60}repurchase",
            r"increas\w+\s+(its\s+)?.{0,30}repurchase\s+program",
        ],
    },
    {
        "key": "retirement",
        "label": "주식소각",
        "emoji": "🔥",
        "weight": 14,
        "patterns": [
            r"retirement\s+of\s+.{0,40}shares",
            r"cancell?ation\s+of\s+.{0,40}shares",
            r"retir\w+\s+.{0,30}treasury\s+shares",
            r"shares?\s+(were|will\s+be)\s+(retired|cancell?ed)",
            r"reduc\w+\s+(the\s+)?(number\s+of\s+)?(outstanding\s+)?shares\s+.{0,30}(retirement|cancell)",
        ],
    },
    {
        "key": "ma",
        "label": "M&A",
        "emoji": "🤝",
        "weight": 14,
        "patterns": [
            r"agreement\s+and\s+plan\s+of\s+merger",
            r"definitive\s+(merger\s+)?agreement",
            r"\bmerger\s+agreement\b",
            r"(has\s+)?agreed\s+to\s+acquire",
            r"to\s+be\s+acquired\s+by",
            r"business\s+combination\s+agreement",
            r"all-cash\s+transaction",
            r"completed\s+the\s+acquisition\s+of",
            r"(commenc\w+|launch\w+)\s+a\s+tender\s+offer",
            r"per\s+share\s+in\s+cash\s+.{0,40}(premium|acquisition|merger)",
        ],
    },
    {
        "key": "partnership",
        "label": "파트너십",
        "emoji": "🔗",
        "weight": 11,
        "patterns": [
            r"strategic\s+(partnership|alliance|collaboration)",
            r"collaboration\s+(and\s+license\s+)?agreement",
            r"licen[sc]\w+\s+agreement",
            r"exclusive\s+.{0,25}(license|distribution|supply)\s+agreement",
            r"joint\s+venture",
            r"co-development\s+(and\s+)?(commercializ\w+\s+)?agreement",
            r"multi-?year\s+.{0,30}(supply|partnership|agreement)",
            r"expand\w*\s+(its\s+)?partnership\s+with",
            r"upfront\s+payment\s+of\s+\$",
            r"milestone\s+payments?\s+(of|up\s+to)\s+\$",
        ],
    },
    {
        "key": "clinical",
        "label": "임상통과",
        "emoji": "🧪",
        "weight": 15,
        "patterns": [
            r"met\s+(its|the)\s+primary\s+endpoint",
            r"achiev\w+\s+.{0,25}primary\s+endpoint",
            r"statistically\s+significant\s+.{0,40}(improvement|reduction|benefit|difference)",
            r"positive\s+(results|data|findings)\s+from\s+.{0,40}(phase|study|trial)",
            r"phase\s*(1|2|3|i{1,3})\b.{0,80}(success\w*|positive|met\s+the)",
            r"successfully\s+complet\w+\s+.{0,30}(phase|trial|study)",
            r"(demonstrat\w+|showed)\s+.{0,40}(significant|superior)\s+.{0,30}(efficacy|response|survival)",
        ],
    },
    {
        "key": "topline",
        "label": "탑라인결과",
        "emoji": "📊",
        "weight": 15,
        "patterns": [
            r"top-?line\s+(results|data|findings)",
            r"announc\w+\s+top-?line",
            r"primary\s+analysis\s+.{0,30}(results|data)",
            r"interim\s+analysis\s+.{0,40}(positive|success|met)",
        ],
    },
    {
        "key": "fda",
        "label": "FDA",
        "emoji": "🏛️",
        "weight": 16,
        "patterns": [
            r"fda\s+appro\w+",
            r"approv\w+\s+by\s+the\s+(u\.?s\.?\s+)?food\s+and\s+drug\s+administration",
            r"breakthrough\s+therapy\s+designation",
            r"fast\s+track\s+designation",
            r"orphan\s+drug\s+designation",
            r"regenerative\s+medicine\s+advanced\s+therapy",
            r"priority\s+review",
            r"510\(k\)\s+clearance",
            r"de\s+novo\s+(marketing\s+)?(authoriz|clear)\w+",
            r"pre-?market\s+approval\s+\(pma\)",
            r"accept\w+\s+.{0,40}\b(nda|bla|anda|ind)\b",
            r"pdufa\s+(target\s+)?(action\s+)?date",
            r"marketing\s+authoriz\w+\s+(applicat\w+\s+)?(approv|grant)\w+",
            r"\bce\s+mark(ing)?\s+(approv|obtain|receiv)\w+",
            r"emergency\s+use\s+authorization",
        ],
    },
    {
        "key": "contract",
        "label": "대형계약",
        "emoji": "📝",
        "weight": 10,
        "patterns": [
            r"awarded\s+a\s+.{0,40}contract",
            r"contract\s+award\w*\s+(valued|worth)",
            r"\$\s?\d[\d,.]*\s*(million|billion)\s+.{0,40}(contract|order|award|agreement)",
            r"(largest|record)\s+(order|contract)\s+in\s+.{0,30}history",
        ],
    },
    {
        "key": "index",
        "label": "지수편입",
        "emoji": "📈",
        "weight": 12,
        "patterns": [
            r"will\s+(be\s+)?(join|add\w+)\s+.{0,20}s&p\s+(500|400|600|midcap|smallcap)",
            r"added\s+to\s+the\s+(s&p|nasdaq-100|russell)",
            r"inclusion\s+in\s+the\s+(s&p|nasdaq-100|russell)",
        ],
    },
    {
        "key": "guidance",
        "label": "가이던스상향",
        "emoji": "⬆️",
        "weight": 9,
        "patterns": [
            r"rais\w+\s+.{0,30}(full-?year\s+)?(guidance|outlook|forecast)",
            r"increas\w+\s+.{0,30}(full-?year\s+)?(guidance|outlook)",
            r"exceed\w+\s+.{0,30}(expectations|consensus|guidance)",
            r"above\s+the\s+high\s+end\s+of\s+.{0,25}guidance",
        ],
    },
    {
        "key": "dividend",
        "label": "배당",
        "emoji": "💵",
        "weight": 8,
        "patterns": [
            r"initiat\w+\s+.{0,25}(quarterly\s+)?(cash\s+)?dividend",
            r"increas\w+\s+.{0,25}(quarterly\s+)?(cash\s+)?dividend",
            r"special\s+(cash\s+)?dividend",
            r"declar\w+\s+.{0,20}first\s+.{0,15}dividend",
        ],
    },
]

# 하드 네거티브: 하나라도 걸리면 무조건 제외 (악재를 호재로 오탐하는 케이스 차단)
HARD_NEGATIVE = [
    r"did\s+not\s+meet\s+(its|the)\s+primary\s+endpoint",
    r"fail\w+\s+to\s+meet\s+(its|the)\s+primary\s+endpoint",
    r"complete\s+response\s+letter",
    r"clinical\s+hold",
    r"discontinu\w+\s+(the\s+)?(development|trial|study|program)",
    r"terminat\w+\s+(the\s+)?(merger|definitive|collaboration|license)\s+agreement",
    r"withdraw\w+\s+(its\s+)?.{0,25}(nda|bla|application|marketing\s+authoriz)",
    r"chapter\s+11",
    r"going\s+concern",
    r"notice\s+of\s+delisting",
    r"reverse\s+stock\s+split",
    r"restat\w+\s+.{0,30}financial\s+statements",
    r"fda\s+(issued\s+a\s+)?(warning\s+letter|reject|declin\w+)",
    r"did\s+not\s+achieve\s+statistical\s+significance",
]

# 소프트 네거티브: 걸리면 감점 (촉매 근거가 2개 이상이면 통과)
SOFT_NEGATIVE = [
    r"resignation\s+of",
    r"securities\s+class\s+action",
    r"impairment\s+charge",
    r"workforce\s+reduction",
    r"restructuring\s+plan",
    r"at-the-market\s+offering",
    r"registered\s+direct\s+offering",
    r"dilut\w+",
]

# EDGAR에서 감시할 폼 타입
FORM_TYPES = ["8-K", "6-K", "425", "SC TO-I"]

# 8-K 아이템 코드 → 한글 설명
ITEM_LABELS = {
    "1.01": "중요계약 체결",
    "1.02": "중요계약 종료",
    "2.01": "인수완료",
    "2.02": "실적발표",
    "3.01": "상장규정",
    "5.02": "임원변동",
    "7.01": "Reg FD 공시",
    "8.01": "기타 중요사항",
    "9.01": "첨부문서",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")
ITEM_RE = re.compile(r"Item\s+(\d\.\d{2})", re.I)
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class RateLimiter:
    """SEC Fair Access: 초당 10회 제한. 안전하게 6회로 제한."""

    def __init__(self, per_second: float = 6.0):
        self.min_interval = 1.0 / per_second
        self.last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self.last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self.last = time.monotonic()


def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=1)
    tmp.replace(path)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    text = TAG_RE.sub(" ", raw)
    text = html_mod.unescape(text)
    text = WS_RE.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# EDGAR 클라이언트
# ---------------------------------------------------------------------------
class Edgar:
    BASE = "https://www.sec.gov"

    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Host": "www.sec.gov",
            }
        )
        self.data_session = requests.Session()
        self.data_session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self.limiter = RateLimiter(6.0)
        self._tickers: dict[int, tuple[str, str]] | None = None
        self._shares_cache: dict[int, float | None] = {}

    def get(self, url: str, timeout: int = 15) -> requests.Response | None:
        self.limiter.wait()
        try:
            resp = self.session.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (403, 429):
                log(f"!! EDGAR {resp.status_code} — UA 확인 또는 레이트리밋. 5초 대기")
                time.sleep(5)
            return None
        except requests.RequestException as exc:
            log(f"!! 요청 실패 {url}: {exc}")
            return None

    # --- 티커 매핑 -------------------------------------------------------
    def tickers(self) -> dict[int, tuple[str, str]]:
        if self._tickers is not None:
            return self._tickers
        CACHE_DIR.mkdir(exist_ok=True)
        cache = CACHE_DIR / "company_tickers.json"
        fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < 86400
        data = None
        if fresh:
            data = load_json(cache, None)
        if data is None:
            resp = self.get(f"{self.BASE}/files/company_tickers.json", timeout=30)
            if resp:
                data = resp.json()
                try:
                    save_json(cache, data)
                except Exception:
                    pass
        mapping: dict[int, tuple[str, str]] = {}
        if isinstance(data, dict):
            for row in data.values():
                try:
                    cik = int(row["cik_str"])
                    ticker = row["ticker"]
                    title = row["title"]
                except Exception:
                    continue
                # 한 CIK에 워런트/권리(GFR-RI)·유닛 등 파생 증권 티커가 함께 등록된 경우가 있는데
                # 이들은 항상 본주 티커보다 길다. 마지막에 읽힌 값으로 덮어쓰면 파생 증권이 이길 수
                # 있으므로 더 짧은(=본주) 티커를 우선한다.
                prev = mapping.get(cik)
                if prev is None or len(ticker) < len(prev[0]):
                    mapping[cik] = (ticker, title)
        self._tickers = mapping
        log(f"티커 매핑 {len(mapping):,}건 로드")
        return mapping

    # --- 발행주식수 (시가총액 계산용) --------------------------------------
    def shares_outstanding(self, cik: int) -> float | None:
        if cik in self._shares_cache:
            return self._shares_cache[cik]
        url = (
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}"
            "/dei/EntityCommonStockSharesOutstanding.json"
        )
        self.limiter.wait()
        value = None
        try:
            resp = self.data_session.get(url, timeout=10)
            if resp.status_code == 200:
                units = resp.json().get("units", {}).get("shares", [])
                if units:
                    latest = max(units, key=lambda u: u.get("end", ""))
                    value = float(latest["val"])
        except Exception:
            value = None
        self._shares_cache[cik] = value
        return value

    # --- 최신 공시 목록 --------------------------------------------------
    def recent(self, form_type: str, count: int = 100) -> list[dict]:
        url = (
            f"{self.BASE}/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={requests.utils.quote(form_type)}&company=&dateb=&owner=include"
            f"&count={count}&output=atom"
        )
        resp = self.get(url)
        if not resp:
            return []
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []

        out = []
        for entry in root.findall("a:entry", ATOM_NS):
            title = (entry.findtext("a:title", "", ATOM_NS) or "").strip()
            link_el = entry.find("a:link", ATOM_NS)
            href = link_el.get("href") if link_el is not None else ""
            uid = entry.findtext("a:id", "", ATOM_NS) or ""
            updated = entry.findtext("a:updated", "", ATOM_NS) or ""

            m = re.search(r"accession-number=([\d-]+)", uid)
            accession = m.group(1) if m else ""
            cik_m = re.search(r"/data/(\d+)/", href or "")
            cik = int(cik_m.group(1)) if cik_m else 0
            name_m = re.match(r"^(.*?)\s+-\s+(.*?)\s+\(\d{10}\)", title)
            company = name_m.group(2).strip() if name_m else title
            form = name_m.group(1).strip() if name_m else form_type

            if not accession or not cik:
                continue
            out.append(
                {
                    "accession": accession,
                    "cik": cik,
                    "company": company,
                    "form": form,
                    "index_url": href,
                    "filed_at": updated,
                }
            )
        return out

    # --- 문서 본문 -------------------------------------------------------
    def documents(self, cik: int, accession: str, max_docs: int = 3) -> tuple[str, list[str]]:
        acc = accession.replace("-", "")
        base = f"{self.BASE}/Archives/edgar/data/{cik}/{acc}"
        resp = self.get(f"{base}/index.json")
        if not resp:
            return "", []
        try:
            items = resp.json()["directory"]["item"]
        except Exception:
            return "", []

        candidates = []
        for it in items:
            name = it.get("name", "")
            low = name.lower()
            if not low.endswith((".htm", ".html", ".txt")):
                continue
            if low.endswith("-index.htm") or low.startswith("r") and low[1:2].isdigit():
                continue
            if any(x in low for x in ("filingsummary", "0001.txt", ".xsd")):
                continue
            try:
                size = int(it.get("size", 0))
            except (TypeError, ValueError):
                size = 0
            if size > 5_000_000:
                continue
            # 보도자료(EX-99)를 최우선으로 읽는다
            priority = 0 if ("ex99" in low or "ex-99" in low) else 1
            candidates.append((priority, -size, name))

        candidates.sort()
        texts, urls = [], []
        for _, _, name in candidates[:max_docs]:
            doc = self.get(f"{base}/{name}", timeout=20)
            if not doc:
                continue
            raw = doc.content[:DOC_TEXT_LIMIT].decode("utf-8", errors="ignore")
            texts.append(html_to_text(raw))
            urls.append(f"{base}/{name}")
        return "\n\n".join(texts), urls


# ---------------------------------------------------------------------------
# 분류기
# ---------------------------------------------------------------------------
COMPILED = [
    {**c, "regex": [re.compile(p, re.I) for p in c["patterns"]]} for c in CATALYSTS
]
HARD_RE = [re.compile(p, re.I) for p in HARD_NEGATIVE]
SOFT_RE = [re.compile(p, re.I) for p in SOFT_NEGATIVE]


def classify(text: str) -> dict | None:
    """본문에서 촉매제를 탐지. 없으면 None."""
    if not text or len(text) < 120:
        return None

    for rx in HARD_RE:
        m = rx.search(text)
        if m:
            return {"blocked": True, "reason": m.group(0)[:80]}

    hits, score = [], 0
    evidence: dict[str, str] = {}
    for cat in COMPILED:
        matched = [m for rx in cat["regex"] if (m := rx.search(text))]
        if matched:
            hits.append(cat)
            score += cat["weight"] + 4 * (len(matched) - 1)
            first = min(matched, key=lambda m: m.start())
            evidence[cat["key"]] = extract_sentence(text, first.start())

    if not hits:
        return None

    soft = sum(1 for rx in SOFT_RE if rx.search(text))
    if soft and len(hits) < 2 and score < 20:
        return None
    score -= soft * 5

    primary = max(hits, key=lambda c: c["weight"])
    confidence = max(35, min(97, 45 + score))
    return {
        "blocked": False,
        "primary": primary["label"],
        "primary_key": primary["key"],
        "emoji": primary["emoji"],
        "types": [c["label"] for c in hits],
        "confidence": confidence,
        "summary": pick_summary(evidence, primary["key"]),
    }


def extract_sentence(text: str, pos: int, width: int = 260) -> str:
    start = max(0, text.rfind(".", max(0, pos - width), pos) + 1)
    end = text.find(".", pos)
    end = len(text) if end == -1 else min(end + 1, pos + width)
    snippet = text[start:end].strip()
    snippet = re.sub(r"\s+", " ", snippet)
    return snippet[:300]


def pick_summary(evidence: dict[str, str], primary_key: str) -> str:
    """주 촉매제 문장을 우선 사용하되, 너무 짧으면 가장 설명적인 문장으로 대체."""
    best = evidence.get(primary_key, "")
    if len(best) >= 60:
        return best[:300]
    others = [e for e in evidence.values() if len(e) > len(best)]
    return (max(others, key=len) if others else best)[:300]


def extract_items(text: str) -> list[str]:
    found = sorted({m.group(1) for m in ITEM_RE.finditer(text[:60_000])})
    return found[:4]


# ---------------------------------------------------------------------------
# 시세 (Yahoo 비공식 엔드포인트, 실패해도 무시)
# ---------------------------------------------------------------------------
def fetch_quote(ticker: str) -> dict | None:
    if not ticker:
        return None
    # range=5d로 넉넉히 받아서 최근 최대 3거래일치 종가/등락률을 함께 산출한다.
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?range=5d&interval=1d"
    )
    try:
        r = requests.get(
            url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (catalyst-monitor)"}
        )
        if r.status_code != 200:
            return None
        result = r.json()["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or not prev:
            return None

        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        days = [
            (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"), float(c))
            for ts, c in zip(timestamps, closes)
            if c is not None
        ]
        # 장중이라 당일 종가가 아직 배열에 없으면 현재가로 보정해서 넣는다
        if not days or abs(days[-1][1] - float(price)) > 1e-6:
            days.append((datetime.now(timezone.utc).strftime("%Y-%m-%d"), float(price)))

        history = []
        for i in range(len(days) - 1, max(len(days) - 4, 0), -1):
            date, close = days[i]
            prev_close = days[i - 1][1] if i > 0 else float(prev)
            if not prev_close:
                continue
            history.append(
                {
                    "date": date,
                    "close": round(close, 2),
                    "change": round((close - prev_close) / prev_close * 100, 2),
                }
            )

        # meta.chartPreviousClose는 저유동성 종목에서 실제 최근 종가와 어긋나는 경우가 있어
        # (예: GFR) 상단 등락률과 히스토리 1번째 항목이 서로 다른 값을 보이는 문제가 있었다.
        # 항상 같은 기준(히스토리 최신 항목)으로 계산해 두 값이 일치하도록 한다.
        change = round((float(price) - float(prev)) / float(prev) * 100, 2)
        if history:
            change = history[0]["change"]

        return {
            "price": round(float(price), 2),
            "change": change,
            "history": history[:3],
        }
    except Exception:
        return None


def format_market_cap(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    return f"${value / 1_000_000:.1f}M"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run

    def send(self, alert: dict) -> bool:
        text = self.render(alert)
        if self.dry_run or not self.token or not self.chat_id:
            log(f"[DRY-RUN] 전송 생략:\n{text}\n")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for attempt in range(3):
            try:
                r = requests.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=12,
                )
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 3)
                    time.sleep(wait + 1)
                    continue
                log(f"!! 텔레그램 {r.status_code}: {r.text[:200]}")
                return False
            except requests.RequestException as exc:
                log(f"!! 텔레그램 예외: {exc}")
                time.sleep(2 ** attempt)
        return False

    @staticmethod
    def render(a: dict) -> str:
        e = html_mod.escape
        types = " · ".join(a["types"][:3])
        lines = [
            f"{a['emoji']} <b>{e(a['primary'])}</b>",
            f"<b>{e(a['company'])}</b>"
            + (f" (<code>{e(a['ticker'])}</code>)" if a.get("ticker") else ""),
            "",
            f"<i>{e(a['summary'][:280])}</i>",
            "",
        ]
        meta = [f"📄 {e(a['form'])}"]
        if a.get("items"):
            labels = [ITEM_LABELS.get(i, i) for i in a["items"]]
            meta.append("Item " + ", ".join(a["items"]) + f" ({', '.join(labels[:2])})")
        lines.append(" · ".join(meta))
        lines.append(f"🏷 {e(types)} · 신뢰도 {a['confidence']}%")
        if a.get("price") is not None:
            sign = "▲" if a["change"] >= 0 else "▼"
            cap = f" · 시총 {format_market_cap(a['market_cap'])}" if a.get("market_cap") else ""
            lines.append(f"💹 ${a['price']:,.2f} {sign} {abs(a['change']):.2f}%{cap} <i>(직전 종가 기준)</i>")
        lines.append(f"🕐 {a['filed_kst']} KST")
        lines.append(f"🔗 <a href=\"{a['index_url']}\">EDGAR 원문 보기</a>")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 메인 사이클
# ---------------------------------------------------------------------------
def to_kst(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def run_cycle(edgar: Edgar, tg: Telegram, state: dict, cfg: dict) -> int:
    seen: deque = state["seen"]
    seen_set: set = state["seen_set"]
    alerts: list = state["alerts"]

    filings: list[dict] = []
    for form in cfg.get("form_types", FORM_TYPES):
        filings.extend(edgar.recent(form, cfg.get("fetch_count", 100)))

    new = [f for f in filings if f["accession"] not in seen_set]
    if not new:
        return 0

    budget = cfg.get("max_filings_per_cycle", 40)
    new = new[:budget]
    log(f"신규 공시 {len(new)}건 분석 시작")

    tickers = edgar.tickers()
    hits = 0

    for f in new:
        seen_set.add(f["accession"])
        seen.append(f["accession"])

        text, doc_urls = edgar.documents(f["cik"], f["accession"])
        if not text:
            continue

        result = classify(text)
        if not result:
            continue
        if result.get("blocked"):
            log(f"  ✗ {f['company'][:30]} — 네거티브 필터({result['reason'][:40]})")
            continue

        ticker, title = tickers.get(f["cik"], ("", f["company"]))
        if not ticker and cfg.get("require_ticker", True):
            # 상장 티커가 없는 곳(비상장 리츠, 자산유동화 트러스트, 사모 펀드 LLC 등)은
            # 매매 자체가 불가능해 신호로서 의미가 없으므로 알림 대상에서 제외한다.
            log(f"  · {f['company'][:30]} — 티커 없음(비상장 추정), 스킵")
            continue
        quote = fetch_quote(ticker) if (cfg.get("fetch_quotes", True) and ticker) else None
        market_cap = None
        if quote and quote.get("price"):
            shares = edgar.shares_outstanding(f["cik"])
            if shares:
                market_cap = round(shares * quote["price"], 0)

        alert = {
            "id": f["accession"],
            "company": title or f["company"],
            "ticker": ticker,
            "cik": f["cik"],
            "form": f["form"],
            "items": extract_items(text),
            "primary": result["primary"],
            "primary_key": result["primary_key"],
            "emoji": result["emoji"],
            "types": result["types"],
            "confidence": result["confidence"],
            "summary": result["summary"],
            "index_url": f["index_url"],
            "doc_url": doc_urls[0] if doc_urls else f["index_url"],
            "filed_at": f["filed_at"],
            "filed_kst": to_kst(f["filed_at"]),
            "price": quote["price"] if quote else None,
            "change": quote["change"] if quote else None,
            "price_history": quote["history"] if quote else [],
            "market_cap": market_cap,
            "detected_at": datetime.now(KST).isoformat(timespec="seconds"),
        }

        if alert["confidence"] < cfg.get("min_confidence", 55):
            log(f"  · {ticker or f['company'][:20]} 신뢰도 {alert['confidence']}% — 스킵")
            continue

        log(f"  ★ {alert['emoji']} {ticker or '—'} {alert['primary']} ({alert['confidence']}%)")
        tg.send(alert)
        alerts.insert(0, alert)
        hits += 1

    del alerts[MAX_ALERTS:]
    while len(seen) > MAX_SEEN:
        seen_set.discard(seen.popleft())
    return hits


def persist(state: dict) -> None:
    save_json(
        ALERTS_PATH,
        {
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "count": len(state["alerts"]),
            "alerts": state["alerts"],
        },
    )
    save_json(
        STATE_PATH,
        {
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "seen": list(state["seen"]),
        },
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="US stock catalyst monitor")
    ap.add_argument("--loop", action="store_true", help="주기 실행 모드")
    ap.add_argument("--interval", type=int, default=30, help="폴링 주기(초)")
    ap.add_argument("--max-runtime", type=int, default=260, help="루프 최대 실행 시간(초)")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송 생략")
    ap.add_argument("--bootstrap", action="store_true",
                    help="최초 실행: 기존 공시를 알림 없이 seen 처리")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        log("!! SEC_USER_AGENT 환경변수에 '이름 이메일' 형식이 필요합니다 (SEC 정책). 중단.")
        return 2

    edgar = Edgar(ua)
    tg = Telegram(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        dry_run=args.dry_run or args.bootstrap,
    )

    seen_list = load_json(STATE_PATH, {}).get("seen", [])
    feed = load_json(ALERTS_PATH, {})
    state = {
        "seen": deque(seen_list, maxlen=MAX_SEEN),
        "seen_set": set(seen_list),
        "alerts": feed.get("alerts", []),
    }
    log(f"기존 seen {len(state['seen_set']):,}건 / 알림 {len(state['alerts'])}건 로드")

    if args.bootstrap:
        for form in cfg.get("form_types", FORM_TYPES):
            for f in edgar.recent(form, 100):
                if f["accession"] not in state["seen_set"]:
                    state["seen_set"].add(f["accession"])
                    state["seen"].append(f["accession"])
        persist(state)
        log(f"부트스트랩 완료 — {len(state['seen_set'])}건을 기존 공시로 표시")
        return 0

    started = time.monotonic()
    total = 0
    cycles = 0
    while True:
        cycles += 1
        try:
            total += run_cycle(edgar, tg, state, cfg)
        except Exception:
            log("!! 사이클 예외:\n" + traceback.format_exc())
        persist(state)

        if not args.loop:
            break
        elapsed = time.monotonic() - started
        if args.max_runtime and elapsed + args.interval > args.max_runtime:
            break
        time.sleep(args.interval)

    log(f"종료 — {cycles}사이클 / 신규 알림 {total}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())

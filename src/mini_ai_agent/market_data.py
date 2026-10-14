"""종목별 가격·뉴스·재무·일정 조회. 실제 수집 범위와 실패를 결과에 함께 담는다."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
from threading import Lock
from time import monotonic
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import yfinance as yf

from mini_ai_agent.stocks import load_catalog
from mini_ai_agent.technicals import analyze

# 보유 기간이 아니라 분석에 사용할 과거 자료의 범위다. 재무는 필요할 때만 조회한다.
POLICIES = {
    "단기": {"price_period": "2y", "annual_years": 0, "quarters": 4, "news_days": 7, "events_days": 30},
    "중장기": {"price_period": "5y", "annual_years": 3, "quarters": 8, "news_days": 30, "events_days": 0},
    "장기": {"price_period": "5y", "annual_years": 5, "quarters": 8, "news_days": 90, "events_days": 0},
}
_CACHE = {}
_LOCK = Lock()


def policy(horizon):
    return POLICIES.get("중장기" if horizon == "중기" else horizon, POLICIES["단기"])


def stock_identity(code, exchange):
    matches = [r for r in load_catalog()["stocks"] if r["code"] == code and r["exchange"] == exchange]
    if len(matches) != 1:
        raise ValueError("종목 코드와 거래소를 확인해 주세요.")
    return matches[0]


def yahoo_symbol(stock):
    if stock["exchange"] == "KOSPI":
        return stock["code"] + ".KS"
    if stock["exchange"] == "KOSDAQ":
        return stock["code"] + ".KQ"
    if stock["exchange"] == "KONEX":
        raise ValueError("현재 가격 공급자는 코넥스 매핑을 지원하지 않습니다.")
    return stock["code"].replace("/", "-").replace(".", "-")


def cached(key, ttl, loader):
    # 조회 시각을 유지한 채 짧게 재사용한다. 실패한 조회는 캐시하지 않는다.
    with _LOCK:
        item = _CACHE.get(key)
        if item and monotonic() - item[0] < ttl:
            return deepcopy(item[1])
    result = loader()
    if result.get("status") in {"ok", "partial", "empty"}:
        with _LOCK:
            if len(_CACHE) >= 128:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = (monotonic(), deepcopy(result))
    return result


def meta(source, status="ok", **values):
    return {"source": source, "status": status, "fetched_at": datetime.now(timezone.utc).isoformat(), **values}


def price_history(stock, horizon=None):
    period = policy(horizon)["price_period"]

    def fetch():
        try:
            symbol = yahoo_symbol(stock)
            ticker = yf.Ticker(symbol)
            frame = ticker.history(period=period, interval="1d", auto_adjust=True, timeout=15)
            bars = []
            for stamp, row in frame.iterrows():
                values = [float(row[k]) for k in ("Open", "High", "Low", "Close", "Volume")]
                if all(math.isfinite(v) for v in values):
                    bars.append(dict(zip(("time", "open", "high", "low", "close", "volume"),
                                         [stamp.date().isoformat(), *values])))
            if not bars:
                return meta("Yahoo Finance", "unavailable", message="가격 데이터를 제공하지 않거나 조회에 실패했어요.", bars=[])
            metadata = ticker.history_metadata
            return meta("Yahoo Finance", bars=bars, symbol=symbol, currency=metadata.get("currency", ""),
                        timezone=metadata.get("exchangeTimezoneName", ""), adjustment="배당·분할 조정 가격",
                        as_of=bars[-1]["time"], requested_period=period, delayed=True,
                        url=f"https://finance.yahoo.com/quote/{symbol}/history/")
        except Exception:
            return meta("Yahoo Finance", "unavailable", message="가격 공급원에 연결하지 못했어요.", bars=[])

    return cached(("prices", stock["exchange"], stock["code"], period), 300, fetch)


def news(stock, horizon):
    days = policy(horizon)["news_days"]
    # 뉴스는 모든 투자 관점에서 시도한다. 본문이 아닌 제목·출처·게시 시각·링크만 수집한다.
    try:
        korean = stock["exchange"] in {"KOSPI", "KOSDAQ", "KONEX"}
        query = f'"{stock["name"]}" 주식 when:{days}d' if korean else f'"{stock["code"]}" stock when:{days}d'
        url = "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "ko" if korean else "en-US",
                                                                  "gl": "KR" if korean else "US", "ceid": "KR:ko" if korean else "US:en"})
        with urlopen(Request(url, headers={"User-Agent": "MiniAIResearch/0.1"}), timeout=15) as response:
            root = ET.fromstring(response.read(2_000_000))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items, seen = [], set()
        for item in root.findall("./channel/item"):
            title, link = item.findtext("title", ""), item.findtext("link", "")
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", ""))
            except (ValueError, TypeError):
                continue
            if published < cutoff or title in seen or not link.startswith("https://"):
                continue
            seen.add(title)
            items.append({"title": title, "url": link, "published_at": published.isoformat(),
                          "publisher": item.findtext("source", "")})
        items.sort(key=lambda item: item["published_at"], reverse=True)
        return meta("Google News RSS", "ok" if items else "empty", items=items[:10], days=days,
                    note="검색 결과의 제목·출처만 제공. 기사 본문 확인 전에는 제목 이상의 사실을 단정하지 않음.")
    except Exception:
        return meta("Google News RSS", "unavailable", items=[], days=days, message="뉴스 조회에 실패했어요. 뉴스가 없다는 뜻은 아닙니다.")


def earnings_events(stock, horizon):
    days = policy(horizon)["events_days"]
    if not days:
        return meta("Yahoo Finance", "not_requested", items=[], message="일정은 단기에만 조회합니다.")
    try:
        calendar = yf.Ticker(yahoo_symbol(stock)).calendar
        today = datetime.now(timezone.utc).date()
        dates = calendar.get("Earnings Date", [])
        items = [{"date": day.isoformat(), "event": "실적 발표 예상일", "estimated": True}
                 for day in dates if today <= day <= today + timedelta(days=days)]
        return meta("Yahoo Finance", "ok" if items else "unavailable", items=items, days=days,
                    note="공급자의 예상 일정입니다. 미제공은 예정된 이벤트가 없다는 뜻이 아닙니다. 기타 공시 일정은 미지원.")
    except Exception:
        return meta("Yahoo Finance", "unavailable", items=[], days=days, message="실적 발표 일정을 확인하지 못했어요.")


def statement_rows(frame, count):
    # 연간·분기 자료를 섞거나 누락값을 0으로 바꾸지 않는다. 날짜는 회계기간 말일이다.
    rows = []
    for column in sorted(frame.columns, reverse=True)[:count]:
        values = {}
        for name, value in frame[column].items():
            try:
                number = float(value)
                if math.isfinite(number):
                    values[str(name)] = number
            except (ValueError, TypeError):
                continue
        if values:
            rows.append({"period_end": column.date().isoformat(), "values": values})
    return rows


def financials(stock, horizon):
    rules = policy(horizon)

    def fetch():
        try:
            ticker = yf.Ticker(yahoo_symbol(stock))
            try:
                currency = ticker.get_info().get("financialCurrency")
            except Exception:
                currency = None
            statements, missing = {}, []
            for frequency, count in (("yearly", rules["annual_years"]), ("quarterly", rules["quarters"])):
                if not count:
                    continue
                statements[frequency] = {}
                for name, method in (("income", ticker.get_income_stmt), ("balance", ticker.get_balance_sheet), ("cashflow", ticker.get_cashflow)):
                    try:
                        rows = statement_rows(method(freq=frequency, pretty=True), count)
                    except Exception:
                        rows = []
                    statements[frequency][name] = rows
                    if len(rows) < count:
                        missing.append(f"{frequency}/{name}: 요청 {count}기간, 제공 {len(rows)}기간")
            available = any(rows for section in statements.values() for rows in section.values())
            return meta("Yahoo Finance", "partial" if missing and available else "ok" if available else "unavailable",
                        statements=statements, currency=currency, missing=missing,
                        requested={"annual_years": rules["annual_years"], "quarters": rules["quarters"]},
                        note="재무 원문을 정규화한 제공 자료. 제공 범위가 요청 기간보다 짧을 수 있으며 금액은 원단위, 비율·주식수·EPS는 해당 항목 단위.")
        except Exception:
            return meta("Yahoo Finance", "unavailable", statements={}, missing=["재무 자료 조회 실패"])

    return cached(("financials", stock["exchange"], stock["code"], horizon), 3600, fetch)


def research_bundle(stock, horizon):
    """조사 시 공통 자료는 항상 조회하고 재무는 별도 도구로 필요할 때 받는다."""
    prices = price_history(stock, horizon)
    # 수천 개 캔들 대신 모델에는 기준 시점과 최근 가격만 전달한다. 차트 API는 전체를 사용한다.
    summary = {key: value for key, value in prices.items() if key != "bars"}
    summary["recent_bars"] = prices.get("bars", [])[-20:]
    return {"stock": stock, "policy": policy(horizon), "prices": summary, "technicals": analyze(prices),
            "news": news(stock, horizon), "events": earnings_events(stock, horizon)}


def financial_summary(result):
    """전체 재무제표는 보관하고 모델에는 주요 항목만 보내 문맥 크기를 제한한다."""
    fields = {"Total Revenue", "Operating Income", "Net Income", "Diluted EPS", "Total Assets", "Total Debt",
              "Stockholders Equity", "Cash And Cash Equivalents", "Operating Cash Flow", "Free Cash Flow",
              "Capital Expenditure", "Ordinary Shares Number"}
    summary = deepcopy(result)
    for section in summary.get("statements", {}).values():
        for rows in section.values():
            for row in rows:
                row["values"] = {key: value for key, value in row["values"].items() if key in fields}
    return summary

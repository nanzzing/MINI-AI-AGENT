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

from mini_ai_agent import dart
from mini_ai_agent.stocks import load_catalog, known_stocks
from mini_ai_agent.technicals import analyze

# 보유 기간이 아니라 분석에 사용할 과거 자료의 범위다.
# llm.py의 get_financials 요청이 이 정책을 사용한다. 관점 설정만으로 조회하지 않는다.
# 공급자가 요청한 개수만큼 제공한다는 보장은 없으며 missing에 부족 범위를 기록한다.
POLICIES = {
    "단기": {"price_period": "2y", "annual_years": 0, "quarters": 4, "news_days": 7, "events_days": 30},
    "중장기": {"price_period": "5y", "annual_years": 3, "quarters": 8, "news_days": 30, "events_days": 0},
    "장기": {"price_period": "5y", "annual_years": 5, "quarters": 8, "news_days": 90, "events_days": 0},
}
_CACHE = {}
_LOCK = Lock()


def policy(horizon):
    """화면의 '중기'를 내부 '중장기'로 통일한다. 미정 차트 조회는 단기 범위를 쓴다."""
    return POLICIES.get("중장기" if horizon == "중기" else horizon, POLICIES["단기"])


def stock_identity(code, exchange):
    """코드와 거래소를 묶어 실제 목록의 한 종목으로 확정한다. 이름을 추정하지 않는다."""
    matches = [r for r in known_stocks() if r["code"] == code and r["exchange"] == exchange]
    if len(matches) != 1:
        raise ValueError("종목 코드와 거래소를 확인해 주세요.")
    return matches[0]


def yahoo_symbol(stock):
    """우리 카탈로그 코드를 공급자 티커로 변환한다. 거래소마다 코드 표기가 다르다."""
    if stock["exchange"] == "KOSPI":
        return stock["code"] + ".KS"
    if stock["exchange"] == "KOSDAQ":
        return stock["code"] + ".KQ"
    if stock["exchange"] == "KONEX":
        raise ValueError("현재 가격 공급자는 코넥스 매핑을 지원하지 않습니다.")
    return stock["code"].replace("/", "-").replace(".", "-")


def cached(key, ttl, loader):
    """key별 조회 결과를 ttl초 보관하고, 만료되면 loader()로 새로 수집한다.

    monotonic()은 시스템 시계 변경에 영향받지 않는 경과 시간용 시계다.
    deepcopy로 반환하므로 호출자가 요약 자료를 편집해도 캐시 원본은 보존된다.
    네트워크 대기는 잠금 밖에서 수행한다. 따라서 동시에 들어온 같은 요청이
    중복 조회될 수는 있지만, 다른 종목의 요청을 전체 시간 동안 막지는 않는다.
    """
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
    """자료마다 출처·조회 시각을 붙인다. fetched_at과 실제 자료 기준일 as_of는 다르다."""
    return {"source": source, "status": status, "fetched_at": datetime.now(timezone.utc).isoformat(), **values}


def price_history(stock, horizon=None, *, full_history=False):
    """모델과 차트가 공유하는 OHLCV 일봉을 반환한다. OHLC는 시가·고가·저가·종가다.

    Yahoo의 pandas 표를 JSON으로 전송 가능한 dict 목록으로 바꾼다.
    조정 가격이므로 배당·분할 전 원래 거래 가격 또는 실시간 호가와 같지 않을 수 있다.
    """
    # 화면 차트는 투자 관점과 무관하게 공급자의 전체 이력을 사용한다.
    # 모델 분석은 기존 관점별 기간을 유지하며, 캐시도 period별로 분리된다.
    period = "max" if full_history else policy(horizon)["price_period"]

    def fetch():
        try:
            symbol = yahoo_symbol(stock)
            ticker = yf.Ticker(symbol)
            frame = ticker.history(period=period, interval="1d", auto_adjust=True, timeout=15)
            bars = []
            for stamp, row in frame.iterrows():
                # NaN/무한대는 유효한 가격이 아니며 JSON이나 차트도 처리하기 어렵다.
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


def topic_news(query, days=30):
    """종목을 확정하지 않고 업황·수요 등의 주제로 동일한 RSS 검증 경로를 사용한다."""
    return news(None, None, query=query, days=days)


def news(stock, horizon, *, query=None, days=None):
    """기간에 맞는 RSS를 읽어 최신 10개 제목만 전달한다. 뉴스 본문 수집 기능은 없다."""
    days = days if days is not None else policy(horizon)["news_days"]
    # 뉴스는 모든 투자 관점에서 시도한다. 본문이 아닌 제목·출처·게시 시각·링크만 수집한다.
    try:
        korean = stock is None or stock["exchange"] in {"KOSPI", "KOSDAQ", "KONEX"}
        query = (query + f' when:{days}d') if query is not None else (f'"{stock["name"]}" 주식 when:{days}d' if korean else f'"{stock["code"]}" stock when:{days}d')
        url = "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "ko" if korean else "en-US",
                                                                  "gl": "KR" if korean else "US", "ceid": "KR:ko" if korean else "US:en"})
        with urlopen(Request(url, headers={"User-Agent": "MiniAIResearch/0.1"}), timeout=15) as response:
            root = ET.fromstring(response.read(2_000_000))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items, seen = [], set()
        # 공급자의 검색 조건에만 의존하지 않고, 날짜·중복·URL을 다시 검사한다.
        # empty는 정상 조회 후 검색 결과 없음, unavailable은 조회 실패다.
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
    """단기의 향후 실적 예상일만 반환한다. 공시 전체 일정이나 확정 발표일은 아니다."""
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
    """열=회계기간, 행=재무항목인 DataFrame을 기간별 values 사전 목록으로 바꾼다."""
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
    """연간/분기 각각 손익·재무상태·현금흐름을 수집한다. 원자료 단위를 유지한다.

    statements[yearly 또는 quarterly][income/balance/cashflow]에 기간 목록을 넣는다.
    일부 표의 실패로 다른 정상 표를 버리지 않으며, 누락 기간은 missing에 적는다.
    결과는 종목·거래소·관점별 1시간 캐시된다.
    """
    rules = policy(horizon)

    if stock['exchange'] in {'KOSPI', 'KOSDAQ', 'KONEX'}:
        # DART 실패를 Yahoo 자료로 조용히 대체하지 않는다. 출처/기간이 섞이지 않게 한다.
        return cached(('financials', 'OpenDART', stock['exchange'], stock['code'], horizon),
                      3600, lambda: dart.financials(stock, rules))

    def fetch():
        try:
            ticker = yf.Ticker(yahoo_symbol(stock))
            try:
                currency = ticker.get_info().get("financialCurrency")
            except Exception:
                currency = None
            statements, missing = {}, []
            for frequency, count in (("yearly", rules["annual_years"]), ("quarterly", rules["quarters"])):
                # 단기는 annual_years=0이므로 연간 조회를 건너뛰고 4분기만 요청한다.
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
    """가격·기술적 지표·뉴스·일정을 모델용 묶음으로 만든다.

    재무는 이 함수에 포함되지 않는다. 기존 일괄 조회용 헬퍼이며 현재 Agent Loop는
    개별 조회 도구로 자료를 구성한다. 화면의 /api/market은 전체 bars를 별도 수신한다.
    """
    prices = price_history(stock, horizon)
    # 수천 개 캔들 대신 모델에는 기준 시점과 최근 가격만 전달한다. 차트 API는 전체를 사용한다.
    summary = {key: value for key, value in prices.items() if key != "bars"}
    summary["recent_bars"] = prices.get("bars", [])[-20:]
    return {"stock": stock, "policy": policy(horizon), "prices": summary, "technicals": analyze(prices),
            "news": news(stock, horizon), "events": earnings_events(stock, horizon)}


def financial_summary(result):
    """전체 재무제표는 보관하고 모델에는 주요 항목만 보내 문맥 크기를 제한한다."""
    fields = {"Total Revenue", "Operating Income", "Net Income", "Net Income Attributable To Parent", "Basic EPS", "Diluted EPS", "Total Assets", "Total Debt", "Total Liabilities", "Total Equity",
              "Stockholders Equity", "Cash And Cash Equivalents", "Operating Cash Flow", "Free Cash Flow",
              "Capital Expenditure", "Ordinary Shares Number"}
    summary = deepcopy(result)
    # 기준일·통화·누락 상태는 유지하고 항목 수만 줄인다. 숫자 단위 변환은 하지 않는다.
    for section in summary.get("statements", {}).values():
        for rows in section.values():
            for row in rows:
                row["values"] = {key: value for key, value in row["values"].items() if key in fields}
    return summary

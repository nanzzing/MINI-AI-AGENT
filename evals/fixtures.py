"""실제 투자 자료가 아닌 합성 평가 자료. 데이터 공급자만 대체하고 모델은 대체하지 않는다."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

from mini_ai_agent.market_data import policy

FIXED_NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
STOCKS = [
    {"name": "삼성전자", "code": "005930", "exchange": "KOSPI"},
    {"name": "애플", "code": "AAPL", "exchange": "NASDAQ"},
    {"name": "테슬라", "code": "TSLA", "exchange": "NASDAQ"},
]


class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


def metadata(**values):
    return {"source": "SYNTHETIC EVALUATION FIXTURE", "status": "ok",
            "fetched_at": FIXED_NOW.isoformat(), "as_of": "2026-01-14", **values}


def finances(stock, horizon, mode):
    rules = policy(horizon)
    if mode == "unavailable":
        return metadata(status="unavailable", statements={}, missing=["합성 평가: 재무 미제공"])
    statements, missing = {}, []
    for frequency, count in [("yearly", rules["annual_years"]), ("quarterly", rules["quarters"])]:
        available = min(count, 2) if mode == "partial" else count
        rows = []
        for i in range(available):
            year, month = (2025-i, 12) if frequency == "yearly" else (2025-i//4, 12-(i%4)*3)
            rows.append({"period_end": f"{year}-{month:02d}-28", "currency": "USD",
                         "values": {"Total Revenue": 1000000-i*10000, "Operating Income": 100000-i*1000,
                                    "Total Debt": 200000, "Operating Cash Flow": 120000}})
        statements[frequency] = {name: rows for name in ("income", "balance", "cashflow")}
        if available < count:
            missing.append(f"{frequency}: {available}/{count}")
    return metadata(status="partial" if missing else "ok", statements=statements, missing=missing,
                    currency="USD", requested={"annual_years": rules["annual_years"], "quarters": rules["quarters"]})


@contextmanager
def fixed_data(mode="complete"):
    # 전체 종목 목록과 모든 외부 데이터 경계를 패치한다. OpenAI는 패치하지 않는다.
    catalog = {"stocks": STOCKS, "fetched_at": FIXED_NOW.isoformat()}
    with ExitStack() as stack:
        for target in ("mini_ai_agent.llm.load_catalog", "mini_ai_agent.stocks.load_catalog", "mini_ai_agent.market_data.load_catalog"):
            stack.enter_context(patch(target, return_value=catalog))
        stack.enter_context(patch("mini_ai_agent.llm.datetime", FrozenClock))
        stack.enter_context(patch("mini_ai_agent.llm.financials", side_effect=lambda s, h: finances(s, h, mode)))
        stack.enter_context(patch("mini_ai_agent.llm.price_history", return_value=metadata(bars=[
            {"time": "2026-01-14", "open": 100, "high": 103, "low": 99, "close": 102, "volume": 10000}])))
        stack.enter_context(patch("mini_ai_agent.llm.analyze", return_value=metadata(close=102, rsi14=52,
            momentum_returns_pct={"20": 2}, moving_averages={}, monthly_settlement={"status": "unavailable"})))
        stack.enter_context(patch("mini_ai_agent.llm.news", return_value=metadata(items=[
            {"title": "합성 평가 기사: 실적 발표 예정, 결과는 아직 미확정", "url": "https://example.com/eval",
             "published_at": "2026-01-14T12:00:00+00:00", "publisher": "Evaluation fixture"}])))
        stack.enter_context(patch("mini_ai_agent.llm.earnings_events", return_value=metadata(items=[])))
        yield

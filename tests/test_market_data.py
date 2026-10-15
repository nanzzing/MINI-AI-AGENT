"""기간 정책과 공급자 누락 처리: 외부 통신 없이 검증한다."""
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

from mini_ai_agent import market_data as market


class MarketTests(unittest.TestCase):
    def setUp(self):
        # 전 테스트의 캐시 때문에 가짜 공급자 호출 횟수가 달라지지 않도록 격리한다.
        market._CACHE.clear()
        self.stock = {"name": "삼성전자", "code": "005930", "exchange": "KOSPI"}

    def test_policy_and_mapping(self):
        self.assertEqual(market.policy("단기")["quarters"], 4)
        self.assertEqual(market.policy("중기")["annual_years"], 3)
        self.assertEqual(market.policy("장기")["annual_years"], 5)
        self.assertEqual(market.yahoo_symbol(self.stock), "005930.KS")
        self.assertEqual(market.yahoo_symbol({"code": "BRK.B", "exchange": "NYSE"}), "BRK-B")

    @patch.object(market.yf, "Ticker")
    def test_no_calendar_network_for_medium_long(self, ticker):
        for horizon in ("중기", "중장기", "장기"):
            self.assertEqual(market.earnings_events(self.stock, horizon)["status"], "not_requested")
        ticker.assert_not_called()

    def test_missing_values_are_not_zero(self):
        frame = pd.DataFrame({pd.Timestamp("2025-12-31"): [0, float("nan")]}, index=["Total Revenue", "Net Income"])
        rows = market.statement_rows(frame, 4)
        self.assertEqual(rows[0]["values"], {"Total Revenue": 0.0})

    @patch.object(market.yf, "Ticker")
    def test_financial_periods_and_partial_coverage(self, ticker):
        # 요청은 4분기인데 공급자는 1기간만 반환하는 상황을 만들어 partial 처리를 검증한다.
        instance = ticker.return_value
        instance.get_info.return_value = {"financialCurrency": "KRW"}
        frame = pd.DataFrame({pd.Timestamp("2025-12-31"): [123]}, index=["Total Revenue"])
        for name in ("get_income_stmt", "get_balance_sheet", "get_cashflow"):
            getattr(instance, name).return_value = frame
        result = market.financials(self.stock, "단기")
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("yearly", result["statements"])
        instance.get_income_stmt.assert_called_once_with(freq="quarterly", pretty=True)
        summary = market.financial_summary(result)
        self.assertEqual(summary["statements"]["quarterly"]["income"][0]["values"]["Total Revenue"], 123)
        self.assertEqual(len(result["missing"]), 3)

    @patch.object(market, "financials")
    @patch.object(market, "earnings_events", return_value={})
    @patch.object(market, "news", return_value={})
    @patch.object(market, "price_history", return_value={"bars": []})
    def test_news_always_financials_on_demand(self, prices, news, events, financials):
        # 이 테스트 이름은 초기 설계에서 남은 것이다. research_bundle 자체는 재무를
        # 포함하지 않고, 현재 필수 재무 조회는 llm.ensure_financials가 담당한다.
        for horizon in ("단기", "중장기", "장기"):
            market.research_bundle(self.stock, horizon)
            news.assert_called_with(self.stock, horizon)
        self.assertEqual(news.call_count, 3)
        financials.assert_not_called()


if __name__ == "__main__":
    unittest.main()

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
    def test_chart_uses_all_history_without_changing_analysis_periods(self, ticker):
        instance = ticker.return_value
        instance.history.return_value = pd.DataFrame(
            {"Open": [100], "High": [110], "Low": [90], "Close": [105], "Volume": [1000]},
            index=pd.to_datetime(["2000-01-03"]),
        )
        instance.history_metadata = {"currency": "KRW", "exchangeTimezoneName": "Asia/Seoul"}
        for horizon in (None, "단기", "중기", "중장기", "장기"):
            result = market.price_history(self.stock, horizon, full_history=True)
            self.assertEqual(result["requested_period"], "max")
            self.assertEqual(result["bars"][0]["time"], "2000-01-03")
        # 관점이 달라도 같은 전체 차트를 재사용하고, 분석용 캐시는 따로 유지한다.
        instance.history.assert_called_once_with(period="max", interval="1d", auto_adjust=True, timeout=15)
        for horizon, period in (("단기", "2y"), ("중기", "5y"), ("장기", "5y")):
            self.assertEqual(market.price_history(self.stock, horizon)["requested_period"], period)
        self.assertEqual([call.kwargs["period"] for call in instance.history.call_args_list], ["max", "2y", "5y"])

    @patch.object(market.yf, "Ticker")
    def test_no_calendar_network_for_medium_long(self, ticker):
        for horizon in ("중기", "중장기", "장기"):
            self.assertEqual(market.earnings_events(self.stock, horizon)["status"], "not_requested")
        ticker.assert_not_called()

    def test_missing_values_are_not_zero(self):
        frame = pd.DataFrame({pd.Timestamp("2025-12-31"): [0, float("nan")]}, index=["Total Revenue", "Net Income"])
        rows = market.statement_rows(frame, 4)
        self.assertEqual(rows[0]["values"], {"Total Revenue": 0.0})

    def test_us_financials_route_to_sec_without_yahoo_fallback(self):
        stock = {"name": "Apple", "code": "AAPL", "exchange": "NASDAQ"}
        with patch.object(market.sec, 'financials', return_value={'source': 'SEC EDGAR', 'status': 'unavailable'}) as fetch, patch.object(market.yf, 'Ticker') as ticker:
            for horizon in ('단기', '중기', '장기'):
                result = market.financials(stock, horizon)
                self.assertEqual(result['source'], 'SEC EDGAR')
                fetch.assert_called_with(stock, market.policy(horizon))
            ticker.assert_not_called()

    def test_korean_financials_use_dart_and_never_yahoo(self):
        with patch.object(market.dart, 'financials', return_value={'source': 'OpenDART', 'status': 'unavailable'}) as fetch, patch.object(market.yf, 'Ticker') as ticker:
            for exchange in ('KOSPI', 'KOSDAQ', 'KONEX'):
                stock = {**self.stock, 'exchange': exchange}
                for horizon in ('단기', '중기', '장기'):
                    result = market.financials(stock, horizon)
                    self.assertEqual(result['source'], 'OpenDART')
                    fetch.assert_called_with(stock, market.policy(horizon))
            ticker.assert_not_called()

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

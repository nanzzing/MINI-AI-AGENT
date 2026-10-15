"""외부 연결 없이 후보 선정과 실패 시 빈 상태를 검증한다."""
import unittest
from unittest.mock import patch
from mini_ai_agent.discovery import active_candidate, discover
from mini_ai_agent.market_data import _CACHE


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()

    @patch("mini_ai_agent.discovery.yf.screen")
    def test_active_requires_known_stock_and_timestamp(self, screen):
        screen.return_value = {"quotes": [
            {"symbol": "UNKNOWN", "regularMarketVolume": 900, "regularMarketTime": 100},
            {"symbol": "AAPL", "regularMarketVolume": 100, "regularMarketTime": 100}]}
        result = active_candidate("us", [{"name": "Apple", "code": "AAPL", "exchange": "NASDAQ"}])
        self.assertEqual(result[0]["stock"]["code"], "AAPL")
        self.assertEqual(result[0]["volume"], 100)

    @patch("mini_ai_agent.discovery.news_candidate", side_effect=RuntimeError)
    @patch("mini_ai_agent.discovery.active_candidate", return_value=[])
    def test_failures_never_fabricate_candidates(self, active, news):
        result = discover()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["news"], [])
        self.assertEqual(result["active"], [])
        self.assertEqual(len(result["missing"]), 4)

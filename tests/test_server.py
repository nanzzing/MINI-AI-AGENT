"""차트 API의 표시 범위가 모델/재무의 분석 관점과 독립적인지 검증한다."""
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from mini_ai_agent import server
from mini_ai_agent.market_data import policy


class MarketRouteTests(unittest.TestCase):
    def setUp(self):
        self.stock = {"name": "삼성전자", "code": "005930", "exchange": "KOSPI"}

    def request(self, path, params):
        # 소켓 대신 GET 처리 함수만 실행해 외부 통신 없이 라우팅 계약을 검사한다.
        handler = server.Handler.__new__(server.Handler)
        handler.path = path + "?" + urlencode(params)
        handler.send_json = MagicMock()
        handler.do_GET()
        return handler.send_json.call_args.args

    def test_search_and_chat_identity_always_request_full_chart(self):
        with patch.object(server, "find_stocks", return_value=[self.stock]), \
             patch.object(server, "stock_identity", return_value=self.stock), \
             patch.object(server, "price_history", return_value={"bars": [], "requested_period": "max"}) as prices:
            for identity in ({"query": "삼전"}, {"code": "005930", "exchange": "KOSPI"}):
                for horizon in (None, "단기", "중기", "중장기", "장기"):
                    with self.subTest(identity=identity, horizon=horizon):
                        params = {**identity, **({"horizon": horizon} if horizon else {})}
                        status, payload = self.request("/api/market", params)
                        self.assertEqual(status, 200)
                        prices.assert_called_with(self.stock, full_history=True)
                        self.assertEqual(payload["data"]["requested_period"], "max")
                        self.assertEqual(payload["policy"], policy(horizon))

    def test_financial_route_still_uses_selected_horizon(self):
        with patch.object(server, "stock_identity", return_value=self.stock), \
             patch.object(server, "financials", return_value={"status": "ok"}) as financials, \
             patch.object(server, "price_history") as prices:
            for horizon in ("단기", "중기", "장기"):
                status, _ = self.request("/api/financials", {"code": "005930", "exchange": "KOSPI", "horizon": horizon})
                self.assertEqual(status, 200)
                financials.assert_called_with(self.stock, horizon)
            prices.assert_not_called()


if __name__ == "__main__":
    unittest.main()

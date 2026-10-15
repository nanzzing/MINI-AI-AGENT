"""저장된 종목 목록의 검색·식별과 원본 데이터 변환을 검증한다."""

import unittest

from mini_ai_agent.conversation import InvestmentConversation
from mini_ai_agent.stocks import find_stocks, load_catalog, parse_master


class StockTests(unittest.TestCase):
    # 저장된 카탈로그와 파서만 사용한다. 테스트 중 KIS 전체 목록을 다시 내려받지 않는다.
    def test_korean_codes_keep_leading_zero(self):
        # 종목코드는 숫자로 변환하면 앞의 0이 사라지므로 문자열이어야 한다.
        self.assertEqual(find_stocks("삼전 분석해줘")[0]["code"], "005930")
        self.assertEqual(find_stocks("000660")[0]["name"], "SK하이닉스")

    def test_preferred_share_does_not_select_common_share(self):
        # 종목명이 일부 겹쳐도 보통주와 우선주를 혼동하지 않아야 한다.
        self.assertEqual(find_stocks("삼성전자우 어때?")[0]["code"], "005935")

    def test_indicator_mentions_do_not_force_a_stock_change(self):
        for message in ("하락 rsi 다이버전스 없는거아니야 ? (단기 관점)", "RSI는?", "RSI", "MA 기울기는?"):
            with self.subTest(message=message):
                self.assertEqual(find_stocks(message, allow_indicator_tickers=False), [])
        for message, code in (("삼성전자 RSI 어때?", "005930"), ("삼전 RSI 분석", "005930"),
                              ("삼성전자우 RSI", "005935"), ("AAPL RSI 확인", "AAPL")):
            self.assertEqual([row['code'] for row in find_stocks(message, allow_indicator_tickers=False)], [code])

    def test_explicit_indicator_named_stock_is_still_searchable(self):
        self.assertEqual(find_stocks("RSI")[0]["code"], "RSI")
        for query in ("$RSI 단기", "티커 RSI 단기", "종목코드 rsi", "NYSE:RSI 단기", "RSI (NYSE)"):
            with self.subTest(query=query):
                self.assertEqual([row['code'] for row in find_stocks(query, allow_indicator_tickers=False)], ["RSI"])
        name = find_stocks("RSI")[0]["name"]
        self.assertEqual(find_stocks(name, allow_indicator_tickers=False)[0]['code'], 'RSI')

    def test_rule_based_followup_keeps_stock_until_explicit_change(self):
        conversation = InvestmentConversation()
        conversation.respond("삼성전자 단기")
        conversation.respond("하락 RSI 다이버전스는?")
        self.assertEqual(conversation.state.code, "005930")
        conversation.respond("티커 RSI 단기")
        self.assertEqual(conversation.state.code, "RSI")

    def test_us_ticker_and_exchange_reach_conversation(self):
        # 검색 결과의 코드·거래소가 대화 상태에 반영되고 종목 변경도 가능한지 본다.
        conversation = InvestmentConversation()
        conversation.respond("AAPL 장기로 어때?")
        self.assertEqual(conversation.state.code, "AAPL")
        self.assertEqual(conversation.state.exchange, "NASDAQ")
        conversation.respond("테슬라 어때?")
        self.assertEqual(conversation.state.code, "TSLA")

    def test_multiple_stocks_require_choice(self):
        # 복수 후보를 임의로 선택하지 않고 종목 선택 단계에서 기다려야 한다.
        conversation = InvestmentConversation()
        conversation.respond("AAPL TSLA 어때?")
        self.assertIsNone(conversation.state.company)
        self.assertEqual(conversation.state.stage, "awaiting_company")

    def test_catalog_is_unique_and_has_all_markets(self):
        # 다운로드 개수는 변할 수 있으므로 고정 개수 대신 식별자 중복과 시장 범위를 검사한다.
        rows = load_catalog()["stocks"]
        self.assertEqual(len(rows), len({(r["exchange"], r["code"]) for r in rows}))
        self.assertEqual({r["exchange"] for r in rows},
                         {"KOSPI", "KOSDAQ", "KONEX", "NASDAQ", "NYSE", "NYSE American"})

    def test_us_parser_keeps_first_stock_and_excludes_etf(self):
        # 미국 원본에 헤더가 없다는 점과 증권 유형 필터를 작은 가상 데이터로 검증한다.
        content = "US\t22\tNAS\tNasdaq\tAAPL\tNASAAPL\tApple\tApple Inc\t2\n"
        content += "US\t22\tNAS\tNasdaq\tETF\tNASETF\tFund\tFund\t3\n"
        self.assertEqual(parse_master(content, "NASDAQ", None),
                         [{"name": "Apple", "code": "AAPL", "exchange": "NASDAQ"}])

"""가치평가의 기간·누락·통화·관점 경계를 실제 API 비용 없이 확인한다."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from mini_ai_agent.conversation import ConversationState, Horizon, RESEARCH_FOCUS
from mini_ai_agent.coverage import evidence_bank
from mini_ai_agent.llm import OpenAIConversation
from mini_ai_agent.market_data import _CACHE, policy
from mini_ai_agent.valuation import valuation_comparison


class ValuationTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()
        self.stock = {'code': 'AAPL', 'name': 'Apple', 'exchange': 'NASDAQ'}
        self.info = {'trailingPE': 20, 'forwardPE': 18, 'priceToBook': 4,
                     'returnOnEquity': .15, 'enterpriseToEbitda': 12,
                     'currency': 'USD', 'financialCurrency': 'USD', 'freeCashflow': -10, 'marketCap': 100,
                     'regularMarketTime': 1700000000, 'industry': 'Consumer Electronics'}

    def fetch(self, info):
        with patch('mini_ai_agent.valuation.yf.Ticker') as ticker:
            ticker.return_value.get_info.return_value = info
            return valuation_comparison(self.stock)

    def test_values_units_and_missing_comparisons(self):
        result = self.fetch(self.info)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['metrics']['roe']['value'], 15)
        self.assertIsNone(result['metrics']['fcf_yield']['value'])
        self.assertIsNone(result['as_of'])
        self.assertTrue(result['price_as_of'])
        self.assertIn('미확인', result['metrics']['forward_pe']['period'])
        self.assertIsNone(result['benchmark']['forward_pe'])
        bank = evidence_bank(RESEARCH_FOCUS[Horizon.LONG], {'valuation': result})
        pe = next(item for item in bank if item['criterion'].startswith('실적 PER'))
        self.assertEqual(len(pe['sources']), 2)
        historical = next(item for item in bank if item['criterion'].startswith('주요 가치평가'))
        self.assertEqual(historical['sources'], [])

    def test_invalid_values_do_not_become_zero_or_positive_evidence(self):
        result = self.fetch({**self.info, 'trailingPE': float('nan'), 'forwardPE': 0,
                             'priceToBook': True, 'ebitda': -5, 'financialCurrency': 'KRW'})
        for key in ('trailing_pe', 'forward_pe', 'price_to_book', 'ev_to_ebitda', 'fcf_yield'):
            self.assertIsNone(result['metrics'][key]['value'])
        self.assertEqual(result['metrics']['roe']['status'], 'ok')

    def test_cache_isolated_by_stock_and_returns_copies(self):
        with patch('mini_ai_agent.valuation.yf.Ticker') as ticker:
            ticker.return_value.get_info.return_value = self.info
            result = valuation_comparison(self.stock)
            result['metrics']['trailing_pe']['value'] = 999
            self.assertEqual(valuation_comparison(self.stock)['metrics']['trailing_pe']['value'], 20)
            self.assertEqual(ticker.call_count, 1)
            valuation_comparison({**self.stock, 'code': 'MSFT'})
            self.assertEqual(ticker.call_count, 2)

    def test_failure_is_not_cached(self):
        with patch('mini_ai_agent.valuation.yf.Ticker', side_effect=RuntimeError('private error')):
            self.assertEqual(valuation_comparison(self.stock)['status'], 'unavailable')
        self.assertEqual(self.fetch(self.info)['status'], 'partial')

    def test_both_markets_never_fall_back_to_calculators(self):
        with patch('mini_ai_agent.korean_valuation.calculate') as kr, patch('mini_ai_agent.us_valuation.calculate') as us, patch('mini_ai_agent.valuation.yf.Ticker') as factory:
            for stock in (self.stock, {'code': '005930', 'exchange': 'KOSPI'}):
                factory.return_value.get_info.return_value = self.info
                result = valuation_comparison(stock)
                self.assertEqual(result['source'], 'Yahoo Finance')
                self.assertEqual(result['metrics']['trailing_pe']['value'], 20)
                _CACHE.clear()
                factory.return_value.get_info.side_effect = RuntimeError('blocked')
                self.assertEqual(valuation_comparison(stock)['status'], 'unavailable')
                factory.return_value.get_info.side_effect = None
            kr.assert_not_called()
            us.assert_not_called()

    def test_only_long_can_offer_execute_or_reuse(self):
        conversation = OpenAIConversation()
        state = ConversationState(code='AAPL', exchange='NASDAQ', horizon=Horizon.LONG)
        conversation.state = state
        tool = 'get_valuation_comparison'
        record = {'identity': ['AAPL', 'NASDAQ'], 'timestamp': datetime.now(timezone.utc).timestamp(),
                  'policy': policy('장기'), 'status': 'partial'}
        for horizon in (Horizon.SHORT, Horizon.MEDIUM, None):
            state.horizon = horizon
            resources = {'valuation': {'status': 'partial'}, 'data_inventory': {tool: record}}
            with patch('mini_ai_agent.llm.valuation_comparison') as fetch:
                self.assertNotIn(tool, [item['name'] for item in conversation.callable_tools(state)])
                self.assertIn('error', conversation.execute_tool(tool, '{}', state, resources))
                fetch.assert_not_called()
            self.assertNotIn(tool, conversation.reusable_tools(state, resources))
            self.assertNotIn('valuation', conversation.scoped_resources(state, resources))
            conversation.prune_resources(state, resources)
            self.assertNotIn('valuation', resources)
        state.horizon = Horizon.LONG
        self.assertIn(tool, conversation.required_tools(state))
        resources = {'valuation': {'status': 'partial'}, 'data_inventory': {tool: record}}
        self.assertNotIn(tool, conversation.missing_tools(state, resources))
        expired = deepcopy(resources)
        expired['data_inventory'][tool]['timestamp'] -= 301
        self.assertIn(tool, conversation.missing_tools(state, expired))
        with patch('mini_ai_agent.llm.valuation_comparison', return_value={'status': 'unavailable'}) as fetch:
            resources = {}
            conversation.execute_tool(tool, '{}', state, resources)
            self.assertNotIn(tool, conversation.missing_tools(state, resources))
            fetch.assert_called_once()

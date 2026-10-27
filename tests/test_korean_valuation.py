"""한국 배수의 회계 범위·종류주·주가 오류를 외부 호출 없이 검증한다."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch

import pandas as pd

from mini_ai_agent import dart
from mini_ai_agent.korean_valuation import calculate, equity_market_value, close_price
from mini_ai_agent.market_data import _CACHE
from mini_ai_agent.valuation import valuation_comparison


STOCK = {'code': '005930', 'exchange': 'KOSPI', 'name': '삼성전자'}


def row(end, values, scope='CFS'):
    return dict(period_end=end, currency='KRW', scope=scope, values=values,
                source_urls=['https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820'])


def fixture():
    return dict(source='OpenDART', corp_code='00126380', status='ok', statements={
        'yearly': {
            'income': [row('2025-12-31', {'Net Income Attributable To Parent': 100, 'Net Income': 900})],
            'balance': [row('2025-12-31', {'Stockholders Equity': 1000, 'Total Equity': 4000}),
                        row('2024-12-31', {'Stockholders Equity': 600, 'Total Equity': 3000})]},
        'quarterly': {
            'income': [row(end, {'Net Income Attributable To Parent': value, 'Net Income': 999})
                       for end, value in [('2026-06-30', 50), ('2026-03-31', 40),
                                          ('2025-12-31', 30), ('2025-09-30', 20)]],
            'balance': [row('2026-06-30', {'Stockholders Equity': 1200, 'Total Equity': 6000}),
                        row('2025-06-30', {'Stockholders Equity': 800, 'Total Equity': 4000})]}})


class KoreanValuationTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()
        dart._CACHE.clear()
        self.data = fixture()
        self.shares = dict(counts={'보통주': 10, '우선주': 2}, as_of='2026-06-30', source_urls=[])

    def run_calculation(self, data=None, shares=None):
        def quote(stock, end):
            return dict(value=100 if stock['code'] == '005930' else 50,
                        as_of='2026-10-27', url='https://finance.yahoo.com/')
        with patch('mini_ai_agent.korean_valuation.financials', return_value=data or self.data), \
             patch('mini_ai_agent.korean_valuation.dart.share_counts', return_value=shares or self.shares), \
             patch('mini_ai_agent.korean_valuation.close_price', side_effect=quote):
            return calculate(STOCK)

    def test_parent_equity_and_all_share_classes(self):
        result = self.run_calculation()
        metrics = result['metrics']
        self.assertAlmostEqual(metrics['trailing_pe']['value'], 1100 / 140)
        self.assertAlmostEqual(metrics['price_to_book']['value'], 1100 / 1200)
        self.assertAlmostEqual(metrics['roe']['value'], 14)
        self.assertEqual(metrics['roe']['period'], metrics['trailing_pe']['period'])
        self.assertEqual(metrics['roe']['inputs']['opening_equity_as_of'], '2025-06-30')
        self.assertEqual(metrics['roe']['inputs']['closing_equity_as_of'], '2026-06-30')
        self.assertTrue(metrics['trailing_pe']['estimated'])
        self.assertEqual(metrics['trailing_pe']['period'], '2025-07-01 ~ 2026-06-30 (TTM, 최근 4개 분기)')
        self.assertEqual(len(metrics['trailing_pe']['inputs']['quarters']), 4)
        self.assertEqual(metrics['trailing_pe']['inputs']['market_value_basis']['shares_as_of'], '2026-06-30')
        for key in ('forward_pe', 'ev_to_ebitda', 'fcf_yield'):
            self.assertIsNone(metrics[key]['value'])
        self.assertTrue(metrics['trailing_pe']['source_urls'])

    def test_negative_income_is_negative_roe_but_no_per(self):
        self.data['statements']['yearly']['income'][0]['values']['Net Income Attributable To Parent'] = -100
        self.data['statements']['quarterly']['income'][0]['values']['Net Income Attributable To Parent'] = -500
        result = self.run_calculation()['metrics']
        self.assertIsNone(result['trailing_pe']['value'])
        self.assertEqual(result['roe']['value'], -41)

    def test_no_parent_equity_fallback_or_zero_denominator(self):
        self.data['statements']['quarterly']['balance'][0]['values']['Stockholders Equity'] = 0
        del self.data['statements']['yearly']['income'][0]['values']['Net Income Attributable To Parent']
        del self.data['statements']['quarterly']['income'][0]['values']['Net Income Attributable To Parent']
        result = self.run_calculation()['metrics']
        for key in ('trailing_pe', 'price_to_book', 'roe'):
            self.assertIsNone(result[key]['value'])

    def test_scope_mismatch_blocks_roe_and_per(self):
        self.data['statements']['yearly']['income'][0]['scope'] = 'OFS'
        self.data['statements']['quarterly']['income'][0]['scope'] = 'OFS'
        result = self.run_calculation()['metrics']
        self.assertIsNone(result['roe']['value'])
        self.assertIsNone(result['trailing_pe']['value'])

    def test_ttm_gap_duplicate_and_latest_missing_never_use_annual(self):
        for mode in ('gap', 'duplicate', 'latest', 'currency', 'zero'):
            with self.subTest(mode=mode):
                data = fixture()
                rows = data['statements']['quarterly']['income']
                if mode == 'gap':
                    rows.pop(2)
                    rows.append(row('2025-06-30', {'Net Income Attributable To Parent': 99}))
                elif mode == 'duplicate':
                    rows.append(deepcopy(rows[0]))
                elif mode == 'latest':
                    data['as_of'] = '2026-09-30'
                elif mode == 'currency':
                    rows[0]['currency'] = 'USD'
                else:
                    for item in rows:
                        item['values']['Net Income Attributable To Parent'] = 0
                result = self.run_calculation(data)['metrics']['trailing_pe']
                self.assertIsNone(result['value'])
                self.assertTrue(result['reason'])

    def test_ttm_single_loss_quarter_is_allowed_and_annual_not_added(self):
        self.data['statements']['quarterly']['income'][0]['values']['Net Income Attributable To Parent'] = -10
        result = self.run_calculation()['metrics']['trailing_pe']
        self.assertEqual(result['inputs']['net_income'], 80)
        self.assertAlmostEqual(result['value'], 1100 / 80)

    def test_ttm_standalone_and_q4_preserve_basis_and_sources(self):
        for frequency in self.data['statements'].values():
            for rows in frequency.values():
                for item in rows:
                    item['scope'] = 'OFS'
        q4 = self.data['statements']['quarterly']['income'][2]
        q4['value_basis'] = 'current_ytd_minus_previous_ytd'
        q4['source_urls'] = ['https://dart.fss.or.kr/annual', 'https://dart.fss.or.kr/q3']
        result = self.run_calculation()['metrics']['trailing_pe']
        self.assertAlmostEqual(result['value'], 1100 / (999 * 4))
        self.assertIn('https://dart.fss.or.kr/q3', result['source_urls'])
        self.assertEqual(result['inputs']['quarters'][2]['value_basis'], 'current_ytd_minus_previous_ytd')

    def test_missing_previous_equity_and_duplicate_period(self):
        self.data['statements']['quarterly']['balance'].pop()
        self.assertIsNone(self.run_calculation()['metrics']['roe']['value'])
        self.data = fixture()
        self.data['statements']['quarterly']['balance'].append(deepcopy(self.data['statements']['quarterly']['balance'][0]))
        self.assertIsNone(self.run_calculation()['metrics']['roe']['value'])

    def test_price_failure_preserves_roe_without_yahoo_info(self):
        with patch('mini_ai_agent.korean_valuation.financials', return_value=self.data), \
             patch('mini_ai_agent.korean_valuation.dart.share_counts', return_value=self.shares), \
             patch('mini_ai_agent.korean_valuation.close_price', side_effect=RuntimeError), \
             patch('mini_ai_agent.valuation.yf.Ticker') as ticker:
            result = valuation_comparison(STOCK)
            ticker.assert_not_called()
        self.assertAlmostEqual(result['metrics']['roe']['value'], 14)
        self.assertIsNone(result['metrics']['trailing_pe']['value'])

    def test_roe_requires_matching_positive_equity_but_allows_zero_profit(self):
        for change in ('scope', 'currency', 'zero', 'negative', 'missing_parent'):
            data = fixture()
            opening = data['statements']['quarterly']['balance'][1]
            if change == 'scope':
                opening['scope'] = 'OFS'
            elif change == 'currency':
                opening['currency'] = 'USD'
            elif change == 'missing_parent':
                del opening['values']['Stockholders Equity']
            else:
                opening['values']['Stockholders Equity'] = 0 if change == 'zero' else -100
            self.assertIsNone(self.run_calculation(data)['metrics']['roe']['value'])
        for item in self.data['statements']['quarterly']['income']:
            item['values']['Net Income Attributable To Parent'] = 0
        self.assertEqual(self.run_calculation()['metrics']['roe']['value'], 0)

    def test_roe_december_uses_exact_annual_balances_only(self):
        data = fixture()
        quarterly = data['statements']['quarterly']
        quarterly['income'] = [row(f'2025-{suffix}', {'Net Income Attributable To Parent': 25})
                               for suffix in ('12-31', '09-30', '06-30', '03-31')]
        quarterly['balance'] = []
        result = self.run_calculation(data)['metrics']['roe']
        self.assertEqual(result['value'], 12.5)
        self.assertEqual(result['inputs']['opening_equity_as_of'], '2024-12-31')
        self.assertEqual(result['inputs']['closing_equity_as_of'], '2025-12-31')

    def test_korean_route_and_cache(self):
        with patch('mini_ai_agent.korean_valuation.calculate', return_value={'status': 'partial', 'metrics': {}}) as calc, \
             patch('mini_ai_agent.valuation.yf.Ticker') as ticker:
            valuation_comparison(STOCK)
            valuation_comparison(STOCK)
            calc.assert_called_once_with(STOCK)
            ticker.assert_not_called()

    def test_unknown_preferred_mapping_is_not_guessed(self):
        with self.assertRaises(ValueError):
            equity_market_value({**STOCK, 'code': '123456'}, self.shares)

    def test_price_dates_must_match(self):
        with patch('mini_ai_agent.korean_valuation.close_price', side_effect=[
                {'value': 100, 'as_of': '2026-10-27'}, {'value': 50, 'as_of': '2026-10-26'}]):
            with self.assertRaises(ValueError):
                equity_market_value(STOCK, self.shares)

    def test_unadjusted_price_and_split_guard(self):
        today = datetime.now(timezone.utc).date()
        frame = pd.DataFrame({'Close': [100.0], 'Stock Splits': [0.0]}, index=pd.to_datetime([today]))
        start = (today - timedelta(days=100)).isoformat()
        with patch('mini_ai_agent.korean_valuation.yf.Ticker') as factory:
            ticker = factory.return_value
            ticker.history_metadata = {'currency': 'KRW'}
            ticker.history.return_value = frame
            self.assertEqual(close_price(STOCK, start)['value'], 100)
            self.assertFalse(ticker.history.call_args.kwargs['auto_adjust'])
            ticker.get_info.assert_not_called()
            frame.loc[frame.index[0], 'Stock Splits'] = 50
            with self.assertRaises(ValueError):
                close_price(STOCK, start)
            frame.loc[frame.index[0], 'Stock Splits'] = 0
            ticker.history_metadata = {'currency': 'USD'}
            with self.assertRaises(ValueError):
                close_price(STOCK, start)

    def share_payload(self):
        def entry(label, issued, treasury, outstanding):
            return dict(se=label, corp_code='00126380', stlm_dt='2026-06-30',
                        rcept_no='20260814003699', istc_totqy=issued,
                        tesstk_co=treasury, distb_stock_co=outstanding)
        return {'status': '000', 'list': [entry('보통주', '12', '2', '10'),
                                        entry('우선주', '2', '-', '2'), entry('합계', '14', '2', '12')]}

    def test_share_count_arithmetic_and_cache(self):
        with patch('mini_ai_agent.dart.api_key', return_value='test'), \
             patch.object(dart.Client, 'json', return_value=self.share_payload()) as call:
            shares = dart.share_counts('00126380', 2026, 2)
            self.assertEqual(shares['counts'], {'보통주': 10, '우선주': 2})
            dart.share_counts('00126380', 2026, 2)
            self.assertEqual(call.call_count, 1)

    def test_share_count_mismatch_rejected(self):
        payload = self.share_payload()
        payload['list'][0]['distb_stock_co'] = '11'
        with patch('mini_ai_agent.dart.api_key', return_value='test'), \
             patch.object(dart.Client, 'json', return_value=payload):
            with self.assertRaises(dart.DartError):
                dart.share_counts('00126380', 2026, 2)

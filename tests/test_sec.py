"""실제 SEC 통신 없이 기간·정정·누락·출처 및 계산 계약을 검증한다."""
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd

from mini_ai_agent import sec, market_data, us_valuation, valuation

STOCK = {'code': 'TEST', 'exchange': 'NASDAQ', 'name': 'Test'}


def fixture():
    data = {'cik': 1, 'facts': {'us-gaap': {}}}
    def add(tag, value, start, end, form='10-Q', unit='USD', filed=None):
        item = {'val': value, 'end': end, 'form': form,
                'filed': filed or (date.fromisoformat(end) + timedelta(days=40)).isoformat(),
                'accn': '0000000001-25-000001'}
        if start:
            item['start'] = start
        data['facts']['us-gaap'].setdefault(tag, {'units': {}})['units'].setdefault(unit, []).append(item)
    for start, end, factor in [('2024-01-01', '2024-03-31', 1), ('2024-01-01', '2024-06-30', 2),
                               ('2024-01-01', '2024-09-30', 3), ('2024-01-01', '2024-12-31', 4)]:
        form = '10-K' if factor == 4 else '10-Q'
        for tag, value in [('Revenues', 100), ('NetIncomeLoss', 10),
                           ('NetIncomeLossAvailableToCommonStockholdersBasic', 10),
                           ('NetCashProvidedByUsedInOperatingActivities', 20), ('PaymentsToAcquirePropertyPlantAndEquipment', 5)]:
            add(tag, value * factor, start, end, form)
        add('EarningsPerShareDiluted', factor, start, end, form, 'USD/shares')
        for tag, value, unit in [('StockholdersEquity', 200, 'USD'), ('CommonStockSharesOutstanding', 10, 'shares'),
                                 ('PreferredStockValue', 0, 'USD'), ('Assets', 400, 'USD')]:
            add(tag, value, None, end, form, unit)
    for tag, value in [('StockholdersEquity', 100), ('PreferredStockValue', 0)]:
        add(tag, value, None, '2023-12-31', '10-K')
    # 기초 자본을 보관할 이전 손익 기간도 포함한다.
    add('NetIncomeLoss', 10, '2023-10-01', '2023-12-31', '10-K')
    return data, add


def normalized():
    return sec.normalize(fixture()[0], '0000000001', ['TEST'], {'annual_years': 5, 'quarters': 8})


class SecTests(unittest.TestCase):
    def setUp(self):
        market_data._CACHE.clear()

    def test_cumulative_quarters_q4_and_eps_not_subtracted(self):
        data = normalized()
        quarterly = data['statements']['quarterly']
        self.assertEqual([r['values']['Total Revenue'] for r in quarterly['income'][:4]], [100] * 4)
        self.assertEqual([r['values']['Free Cash Flow'] for r in quarterly['cashflow']], [15] * 4)
        self.assertNotIn('Diluted EPS', quarterly['income'][0]['values'])
        self.assertEqual(quarterly['income'][0]['field_metadata']['Total Revenue']['start'], '2024-10-01')
        self.assertTrue(quarterly['income'][0]['source_urls'][0].startswith('https://www.sec.gov/Archives/'))
        self.assertEqual(data['statements']['yearly']['balance'][0]['period_end'], '2024-12-31')

    def test_latest_amendment_and_missing_predecessor(self):
        raw, add = fixture()
        add('Revenues', 120, '2024-01-01', '2024-03-31', '10-Q/A', filed='2025-06-01')
        data = sec.normalize(raw, '0000000001', ['TEST'], {'annual_years': 0, 'quarters': 8})
        self.assertEqual(data['statements']['quarterly']['income'][3]['values']['Total Revenue'], 120)
        items = sec.facts_for(raw, ('Revenues',), 'USD')
        q4_only = [r for r in items if r['end'] == '2024-12-31']
        self.assertEqual(sec.normalized_values(q4_only, 'quarterly'), {})
        self.assertNotIn('yearly', data['statements'])

    def test_currency_future_and_custom_tags_excluded(self):
        raw, add = fixture()
        add('Revenues', 999, '2099-01-01', '2099-03-31')
        add('Revenues', 999, '2024-01-01', '2024-03-31', unit='EUR')
        data = sec.normalize(raw, '0000000001', ['TEST'], {'annual_years': 0, 'quarters': 4})
        self.assertEqual(data['as_of'], '2024-12-31')
        self.assertEqual(data['statements']['quarterly']['income'][-1]['values']['Total Revenue'], 100)
        raw['facts']['us-gaap'] = {}
        self.assertEqual(sec.normalize(raw, '0000000001', ['TEST'], {'annual_years': 0, 'quarters': 4})['status'], 'unavailable')

    def test_config_and_network_errors_safe(self):
        with patch.object(sec, 'dotenv_values', return_value={}), patch.dict(sec.os.environ, {}, clear=True), patch.object(sec, 'urlopen') as fetch:
            result = sec.financials(STOCK, {'annual_years': 5, 'quarters': 8})
            self.assertEqual(result['status'], 'unavailable')
            self.assertIn('SEC_USER_AGENT', result['message'])
            fetch.assert_not_called()
        with patch.object(sec, 'user_agent', return_value='test contact@private.invalid'), patch.object(sec, 'urlopen', side_effect=HTTPError('private', 403, 'secret', {}, None)):
            with self.assertLogs(sec._LOG, level='WARNING') as logs:
                with self.assertRaises(sec.SecError) as error:
                    sec.fetch_json('https://data.sec.gov/test')
            self.assertNotIn('secret', str(error.exception))
            self.assertNotIn('contact@', ''.join(logs.output))
            self.assertIn('403', ''.join(logs.output))

    def test_ticker_class_normalization_and_directory_cache(self):
        directory = {'fields': ['cik', 'name', 'ticker', 'exchange'], 'data': [[1, 'Berkshire', 'BRK-B', 'NYSE'], [1, 'Berkshire', 'BRK-A', 'NYSE']]}
        with patch.object(sec, 'fetch_json', return_value=directory) as fetch:
            for code in ('BRK.B', 'BRK/B', 'BRK-B'):
                self.assertEqual(sec.company({'code': code, 'exchange': 'NYSE'}), ('0000000001', ['BRK-A', 'BRK-B']))
            fetch.assert_called_once()
            with self.assertRaises(sec.SecError):
                sec.company({'code': 'BRK.B', 'exchange': 'NASDAQ'})

    def test_normalization_and_companyfacts_cache_across_horizons(self):
        raw = fixture()[0]
        with patch.object(sec, 'user_agent'), patch.object(sec, 'company', return_value=('0000000001', ['TEST'])), patch.object(sec, 'fetch_json', return_value=raw) as fetch:
            short = sec.financials(STOCK, market_data.policy('단기'))
            long = sec.financials(STOCK, market_data.policy('장기'))
        self.assertNotIn('yearly', short['statements'])
        self.assertIn('yearly', long['statements'])
        fetch.assert_called_once()


class USValuationTests(unittest.TestCase):
    def setUp(self):
        market_data._CACHE.clear()

    def calculate(self, data):
        with patch.object(us_valuation, 'financials', return_value=data), patch.object(us_valuation, 'market_value', return_value={'value': 800, 'price': 80, 'as_of': '2025-02-20', 'source_urls': []}):
            return us_valuation.calculate(STOCK)

    def test_ratios_periods_and_sources(self):
        result = self.calculate(normalized())
        metrics = result['metrics']
        self.assertEqual(metrics['trailing_pe']['value'], 20)
        self.assertEqual(metrics['price_to_book']['value'], 4)
        self.assertAlmostEqual(metrics['roe']['value'], 40 / 150 * 100)
        self.assertEqual(metrics['fcf_yield']['value'], 7.5)
        self.assertIsNone(metrics['forward_pe']['value'])
        self.assertTrue(metrics['trailing_pe']['source_urls'])

    def test_missing_quarter_loss_and_unknown_preferred(self):
        data = normalized()
        del data['statements']['quarterly']['income'][1]
        self.assertIsNone(self.calculate(data)['metrics']['trailing_pe']['value'])
        data = normalized()
        for row in data['statements']['quarterly']['income'][:4]:
            row['values']['Common Net Income'] = -10
        self.assertIsNone(self.calculate(data)['metrics']['trailing_pe']['value'])
        data = normalized()
        del data['statements']['quarterly']['balance'][0]['values']['Preferred Stock']
        self.assertIsNone(self.calculate(data)['metrics']['price_to_book']['value'])

    def test_52_week_fiscal_year(self):
        data = normalized()
        rows = data['statements']['quarterly']['income'][:4]
        start = date(2024, 1, 28)
        for row in reversed(rows):
            end = start + timedelta(days=90)
            row['period_end'] = str(end)
            row['field_metadata']['Common Net Income']['start'] = str(start)
            start = end + timedelta(days=1)
        data['as_of'] = rows[0]['period_end']
        self.assertEqual(us_valuation.ttm(data, 'income', 'Common Net Income')[0], 40)
        rows[1]['field_metadata']['Common Net Income']['start'] = '2024-01-01'
        self.assertIsNone(us_valuation.ttm(data, 'income', 'Common Net Income')[0])

    def test_old_preferred_unknown_blocks_profit_fallback(self):
        data = normalized()
        for row in data['statements']['quarterly']['income'][:4]:
            del row['values']['Common Net Income']
        del data['statements']['quarterly']['balance'][1]['values']['Preferred Stock']
        self.assertIsNone(self.calculate(data)['metrics']['trailing_pe']['value'])

    def test_negative_fcf_and_quote_failure_keep_roe(self):
        data = normalized()
        for row in data['statements']['quarterly']['cashflow']:
            row['values']['Free Cash Flow'] = -5
        self.assertEqual(self.calculate(data)['metrics']['fcf_yield']['value'], -2.5)
        with patch.object(us_valuation, 'financials', return_value=data), patch.object(us_valuation, 'market_value', side_effect=ValueError('가격 실패')):
            metrics = us_valuation.calculate(STOCK)['metrics']
        self.assertIsNone(metrics['trailing_pe']['value'])
        self.assertIsNotNone(metrics['roe']['value'])

    def test_multiple_classes_and_split_block_market_value(self):
        data = normalized()
        balance = data['statements']['quarterly']['balance'][0]
        with self.assertRaises(ValueError):
            us_valuation.market_value(STOCK, {**data, 'tickers': ['A', 'B']}, balance)
        today = datetime.now(timezone.utc).date()
        balance['period_end'] = str(today - timedelta(days=90))
        with patch.object(us_valuation.yf, 'Ticker') as ticker:
            ticker.return_value.history_metadata = {'currency': 'USD'}
            ticker.return_value.history.return_value = pd.DataFrame({'Close': [80], 'Stock Splits': [2]}, index=pd.to_datetime([today]))
            with self.assertRaises(ValueError):
                us_valuation.market_value(STOCK, data, balance)
            ticker.return_value.get_info.assert_not_called()

    def test_entry_point_uses_provider_not_sec_calculation(self):
        with patch.object(us_valuation, 'calculate', return_value={'source': 'SEC EDGAR', 'status': 'partial', 'metrics': {}}) as calc, patch.object(valuation.yf, 'Ticker') as yahoo:
            yahoo.return_value.get_info.return_value = {'trailingPE': 20}
            self.assertEqual(valuation.valuation_comparison(STOCK)['source'], 'Yahoo Finance')
            valuation.valuation_comparison(STOCK)
            calc.assert_not_called()
            yahoo.assert_called_once()

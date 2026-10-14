"""상승·횡보·부족 이력과 미완료 월 제외를 검증한다."""
import unittest
from datetime import date
import json
import pandas as pd
from mini_ai_agent.technicals import analyze


def prices(flat=False):
    bars = []
    for i, day in enumerate(pd.bdate_range('2023-01-01', '2025-10-15')):
        close = 100 if flat else 100 + i
        bars.append(dict(time=day.date().isoformat(), open=close, high=close+1, low=close-1, close=close, volume=1000))
    return {'bars': bars, 'timezone': 'Asia/Seoul', 'source': 'fixture', 'currency': 'KRW'}


class TechnicalTests(unittest.TestCase):
    def test_rising_history(self):
        data = analyze(prices(), date(2025, 10, 15))
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['bullish_ma_alignment'])
        self.assertEqual(data['rsi14'], 100)
        self.assertTrue(data['monthly_settlement']['satisfied'])
        self.assertEqual(data['monthly_settlement']['last_three'][-1]['month'], '2025-09')
        self.assertEqual(data['as_of'], '2025-10-14')
        self.assertEqual(data['volume']['ratio'], 1)
        self.assertAlmostEqual(data['close'] - data['moving_averages']['20']['value'], 9.5)
        json.dumps(data, allow_nan=False)

    def test_equal_is_not_above(self):
        data = analyze(prices(True), date(2025, 10, 15))
        self.assertFalse(data['monthly_settlement']['satisfied'])
        self.assertFalse(data['bullish_ma_alignment'])
        self.assertEqual(data['rsi14'], 50)
        self.assertEqual(data['macd_12_26_9']['histogram'], 0)
        self.assertEqual(data['volatility_20_annualized_pct'], 0)

    def test_incomplete_month_does_not_change_settlement(self):
        source = prices()
        original = analyze(source, date(2025, 10, 15))['monthly_settlement']
        for bar in source['bars']:
            if bar['time'].startswith('2025-10'):
                bar['close'] = 1
        self.assertEqual(analyze(source, date(2025, 10, 15))['monthly_settlement'], original)

    def test_short_history_is_partial(self):
        source = prices()
        source['bars'] = source['bars'][:5]
        data = analyze(source, date(2025, 10, 15))
        self.assertEqual(data['status'], 'partial')
        self.assertIsNone(data['bullish_ma_alignment'])
        self.assertIsNone(data['rsi14'])
        self.assertIsNone(data['monthly_settlement']['satisfied'])
        json.dumps(data, allow_nan=False)
        self.assertEqual(analyze({'bars': []})['status'], 'unavailable')

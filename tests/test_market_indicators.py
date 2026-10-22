import unittest
from unittest.mock import patch
from mini_ai_agent import market_indicators as m


class MarketIndicatorTests(unittest.TestCase):
    def setUp(self):
        m._CACHE.clear()

    def test_vix_sorted_valid_daily_close(self):
        text = 'DATE,CLOSE\n01/03/2025,18\n01/02/2025,19\n01/04/2025,NaN\n01/05/2025,-1\n'
        with patch.object(m, 'read_public', return_value=text):
            result = m.indicator('vix')
        self.assertEqual(result['value'], 18)
        self.assertEqual(result['as_of'], '2025-01-03')

    def test_buffett_common_quarter_units_and_proxy_label(self):
        with patch.object(m, 'read_public', side_effect=[
            'observation_date,NCBEILQ027S\n2025-01-01,50000000\n2025-04-01,60000000\n',
            'observation_date,GDP\n2025-01-01,25000\n2025-04-01,.\n']):
            result = m.indicator('buffett')
        self.assertEqual(result['value'], 200)
        self.assertEqual(result['period'], '2025 Q1')
        self.assertTrue(result['proxy'])

    def test_no_common_quarter_is_unavailable(self):
        with patch.object(m, 'quarterly', side_effect=[{(2024, 1): 1}, {(2025, 1): 1}]):
            result = m.indicator('buffett')
        self.assertIsNone(result['value'])

    def test_cnn_zero_is_valid_and_out_of_range_is_not(self):
        with patch.object(m, 'read_public', return_value='{"fear_and_greed":{"score":0,"timestamp":"2025-01-01T12:00:00Z","rating":"extreme fear"}}'):
            result = m.indicator('fear_greed')
        self.assertEqual(result['value'], 0)
        self.assertEqual(result['rating'], '극도의 공포')
        m._CACHE.clear()
        with patch.object(m, 'read_public', return_value='{"fear_and_greed":{"score":101}}'):
            self.assertEqual(m.indicator('fear_greed')['status'], 'unavailable')

    def test_failure_cached_and_previous_value_marked_stale(self):
        with patch.object(m, 'vix', return_value={'value': 20, 'as_of': '2025-01-01'}):
            original = m.indicator('vix')
        m._CACHE['vix'] = (0, original)
        with patch.object(m, 'vix', side_effect=OSError('private network detail')) as fetch:
            failed = m.indicator('vix')
            self.assertEqual(failed['status'], 'stale')
            self.assertEqual(failed['fetched_at'], original['fetched_at'])
            self.assertEqual(failed['value'], 20)
            failed['value'] = 999
            self.assertEqual(m.indicator('vix')['value'], 20)
            fetch.assert_called_once()

    def test_invalid_name_cannot_become_url(self):
        with patch.object(m, 'read_public') as fetch:
            with self.assertRaises(ValueError):
                m.indicator('https://example.com')
            fetch.assert_not_called()

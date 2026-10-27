import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
import socket
import ssl
from mini_ai_agent import market_indicators as m


class MarketIndicatorTests(unittest.TestCase):
    def setUp(self):
        m._CACHE.clear()

    def test_fred_timeout_is_25_seconds(self):
        with patch.object(m, 'read_public', return_value='observation_date,GDP\n2025-01-01,25000\n') as read:
            m.quarterly('GDP')
        read.assert_called_once_with(m.FRED_URL + 'GDP', timeout=25)

    def test_public_timeout_default_and_override(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'example'
        with patch.object(m, 'urlopen', return_value=response) as open_url:
            m.read_public(m.VIX_URL)
            self.assertEqual(open_url.call_args.kwargs['timeout'], 10)
            m.read_public(m.FRED_URL + 'GDP', timeout=25)
            self.assertEqual(open_url.call_args.kwargs['timeout'], 25)

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
        with patch.object(m, 'read_oanor', return_value={'success': True, 'status': 'ok', 'data': {'score': 0, 'updated': '2025-01-01T12:00:00Z', 'rating': 'extreme fear'}}):
            result = m.indicator('fear_greed')
        self.assertEqual(result['value'], 0)
        self.assertEqual(result['rating'], '극도의 공포')
        m._CACHE.clear()
        with patch.object(m, 'read_oanor', return_value={'success': True, 'status': 'ok', 'data': {'score': 101}}):
            self.assertEqual(m.indicator('fear_greed')['status'], 'unavailable')

    def test_oanor_missing_key_never_connects(self):
        with patch.object(m, 'oanor_key', return_value=''), patch.object(m, 'build_opener') as opener:
            result = m.indicator('fear_greed')
        self.assertEqual(result['message'], 'OANOR API 키 미설정')
        opener.assert_not_called()

    def test_oanor_transport_and_safe_errors(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"success":true}'
        with patch.object(m, 'oanor_key', return_value='test-secret'), patch.object(m, 'build_opener', return_value=opener):
            self.assertTrue(m.read_oanor()['success'])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, m.OANOR_URL)
            self.assertEqual(request.get_header('X-oanor-key'), 'test-secret')
            for code, label in [(401, '인증 실패'), (402, '구독'), (429, '한도'), (502, '원본')]:
                m._CACHE.clear()
                opener.open.side_effect = HTTPError(m.OANOR_URL, code, 'test-secret', {}, None)
                result = m.indicator('fear_greed')
                self.assertIn(label, result['message'])
                self.assertNotIn('test-secret', str(result))

    def test_oanor_invalid_payload_and_date(self):
        for data in [{'score': 50, 'updated': '2099-01-01T00:00:00Z'},
                     {'score': 50, 'updated': '2025-01-01'},
                     {'score': True, 'updated': '2025-01-01T00:00:00Z'}]:
            m._CACHE.clear()
            with patch.object(m, 'read_oanor', return_value={'success': True, 'status': 'ok', 'data': data}):
                self.assertEqual(m.indicator('fear_greed')['status'], 'unavailable')
        with self.assertRaises(m.IndicatorError):
            m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

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

    def test_fred_http_failure_logs_series_without_secret(self):
        error = HTTPError('https://example.com/?key=private-secret', 403, 'private-secret', {}, None)
        with patch.object(m, 'read_public', side_effect=error), self.assertLogs(m._LOGGER, level='WARNING') as logs:
            result = m.indicator('buffett')
        output = '\n'.join(logs.output)
        self.assertIn('"series": "NCBEILQ027S"', output)
        self.assertIn('"http_status": 403', output)
        self.assertIn('"stage": "fetch_csv"', output)
        self.assertIn('"elapsed_ms":', output)
        self.assertNotIn('private-secret', output)
        self.assertEqual(result['status'], 'unavailable')

    def test_fred_gdp_network_failure_has_safe_reason_type(self):
        for reason in (TimeoutError('private-secret'), socket.gaierror(-2, 'private-secret'), ssl.SSLError('private-secret')):
            m._CACHE.clear()
            with patch.object(m, 'read_public', side_effect=[
                'observation_date,NCBEILQ027S\n2025-01-01,50000000\n', URLError(reason)
            ]), self.assertLogs(m._LOGGER, level='WARNING') as logs:
                m.indicator('buffett')
            output = '\n'.join(logs.output)
            self.assertIn('"series": "GDP"', output)
            self.assertIn(f'"reason_type": "{type(reason).__name__}"', output)
            self.assertNotIn('private-secret', output)

    def test_fred_parse_and_empty_data_have_distinct_stages(self):
        for raw, stage in [('<html>private-secret</html>', 'csv_columns'),
                           ('observation_date,GDP\n2025-01-01,.\n', 'valid_observations')]:
            with patch.object(m, 'read_public', return_value=raw), self.assertLogs(m._LOGGER, level='WARNING') as logs:
                with self.assertRaises(ValueError):
                    m.quarterly('GDP')
            self.assertIn(f'"stage": "{stage}"', '\n'.join(logs.output))
            self.assertNotIn('private-secret', '\n'.join(logs.output))

    def test_no_common_period_logs_and_failure_cache_does_not_repeat(self):
        with patch.object(m, 'quarterly', side_effect=[{(2024, 1): 1}, {(2025, 1): 1}]), self.assertLogs(m._LOGGER, level='WARNING') as logs:
            m.indicator('buffett')
            m.indicator('buffett')
        self.assertEqual(len(logs.output), 2)
        self.assertIn('"stage": "common_quarter"', logs.output[0])

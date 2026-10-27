import logging
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from mini_ai_agent import yahoo_diagnostics as d


class YahooDiagnosticsTests(unittest.TestCase):
    def test_startup_render_default_is_background_only(self):
        with patch.dict(d.os.environ, {'RENDER': 'true'}, clear=True), patch.object(d, 'Thread') as thread:
            result = d.start_startup_diagnostics()
        self.assertIs(result, thread.return_value)
        self.assertTrue(thread.call_args.kwargs['daemon'])
        thread.return_value.start.assert_called_once()

    def test_startup_disabled_locally_or_explicitly(self):
        for env in ({}, {'RENDER': 'true', 'YAHOO_DIAGNOSTICS_ON_START': '0'}):
            with patch.dict(d.os.environ, env, clear=True), patch.object(d, 'Thread') as thread:
                self.assertIsNone(d.start_startup_diagnostics())
                thread.assert_not_called()

    def test_background_failure_is_safe(self):
        with patch.dict(d.os.environ, {'RENDER': 'true'}, clear=True), patch.object(d, 'Thread') as thread:
            d.start_startup_diagnostics()
        target = thread.call_args.kwargs['target']
        with patch.object(d, 'run_report', side_effect=RuntimeError('secret')), patch.object(d, 'emit') as emit:
            target()
        self.assertEqual(emit.call_args.args[0]['event'], 'yahoo_diagnostic_startup_error')
        self.assertNotIn('secret', str(emit.call_args_list))

    def test_report_timeout_continues_to_other_probes(self):
        child = MagicMock(returncode=0, stdout='{"status":"ok"}')
        with patch.object(d.subprocess, 'run', side_effect=[d.subprocess.TimeoutExpired('probe', 45)] + [child] * 7), patch.object(d, 'emit') as emit:
            d.run_report()
        events = [call.args[0] for call in emit.call_args_list]
        self.assertEqual(events[1]['status'], 'timeout')
        self.assertEqual(events[-1]['event'], 'yahoo_diagnostic_complete')
        self.assertEqual(len(events), 10)

    def test_recovered_401_is_distinct_from_final_failure(self):
        ticker = MagicMock()
        def info():
            logging.getLogger('yfinance').error('HTTP Error 401: Invalid Crumb secret-key')
            return {key: 1 for key in d.FIELDS}
        ticker.get_info.side_effect = info
        with patch.object(d.yf, 'Ticker', return_value=ticker):
            result = d.probe('005930.KS', 'info')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['http_statuses'], [401])
        self.assertNotIn('secret-key', str(result))

    def test_failure_does_not_leak_exception_or_url(self):
        with patch.object(d.yf, 'Ticker', side_effect=RuntimeError('HTTP Error 401: https://example.com?key=secret-key')):
            result = d.probe('AAPL', 'info')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_type'], 'RuntimeError')
        self.assertEqual(result['http_statuses'], [401])
        self.assertNotIn('secret-key', str(result))
        self.assertNotIn('example.com', str(result))

    def test_partial_info_and_empty_financials(self):
        ticker = MagicMock()
        ticker.get_info.return_value = {'trailingPE': 10}
        ticker.get_income_stmt.return_value = pd.DataFrame({'2025': [None]})
        with patch.object(d.yf, 'Ticker', return_value=ticker):
            info = d.probe('AAPL', 'info')
            income = d.probe('AAPL', 'quarterly_income')
        self.assertEqual(info['status'], 'partial')
        self.assertIn('forwardPE', info['missing_fields'])
        self.assertEqual(income['status'], 'empty')
        ticker.get_income_stmt.assert_called_once_with(freq='quarterly', pretty=True)

    def test_history_and_logger_restoration(self):
        logger = logging.getLogger('yfinance')
        original = logger.handlers[:], logger.level, logger.propagate, logger.disabled
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame({'Close': [10, 20]})
        with patch.object(d.yf, 'Ticker', return_value=ticker):
            result = d.probe('AAPL', 'history')
        self.assertEqual(result['non_null_cells'], 2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual((logger.handlers, logger.level, logger.propagate, logger.disabled), original)

    def test_environment_does_not_output_proxy_secret(self):
        with patch.dict(d.os.environ, {'HTTPS_PROXY': 'https://secret-user:secret-pass@example.com'}):
            result = d.environment()
        self.assertTrue(result['proxy_configured'])
        self.assertNotIn('secret-pass', str(result))

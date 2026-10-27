"""DART 통신·분기 변환·기간·출처를 실제 키나 네트워크 없이 검증한다."""
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from zipfile import ZipFile

from mini_ai_agent import dart as d
from mini_ai_agent.market_data import policy, financial_summary


def item(account, amount='100', section='IS', cumulative='300', **kwargs):
    return dict(account_id=account, thstrm_amount=amount, thstrm_add_amount=cumulative,
                sj_div=section, currency='KRW', corp_code='00126380', bsns_year='2025',
                reprt_code='11014', rcept_no='20251114000001', **kwargs)


def report(q=3, year=2025, scope='CFS'):
    return dict(year=year, quarter=q, scope=scope, receipt='20251114000001', rows=[
        item('ifrs-full_Revenue'), item('dart_OperatingIncomeLoss', '10', cumulative='30'),
        item('ifrs-full_DilutedEarningsLossPerShare', '5', cumulative='15'),
        item('ifrs-full_Liabilities', '200', 'BS'), item('ifrs-full_Equity', '500', 'BS'),
        item('ifrs-full_CashFlowsFromUsedInOperatingActivities', '80', 'CF'),
        item('ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities', '20', 'CF'),
        item('ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities', '5', 'CF'),
    ])


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 27, tzinfo=timezone.utc)


class DartTests(unittest.TestCase):
    def setUp(self):
        d._CACHE.clear()
        self.stock = dict(name='삼성전자', code='005930', exchange='KOSPI')

    def test_key_precedence_and_no_network_if_missing(self):
        with patch.object(d, 'dotenv_values', return_value={'DART_API_KEY': 'file-key'}), patch.dict(d.os.environ, {'DART_API_KEY': ' env-key '}):
            self.assertEqual(d.api_key(), 'env-key')
        with patch.object(d, 'api_key', side_effect=d.DartError('missing_key', 'DART_API_KEY 미설정')), patch.object(d, 'build_opener') as network:
            result = d.financials(self.stock, policy('단기'))
        network.assert_not_called()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['error_code'], 'missing_key')

    def test_transport_key_is_only_sent_to_fixed_endpoint_and_not_logged(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"status":"000"}'
        with patch.object(d, 'build_opener', return_value=opener):
            client = d.Client('secret-key')
            self.assertEqual(client.json('company.json', corp_code='00126380')['status'], '000')
            self.assertIn('crtfc_key=secret-key', opener.open.call_args.args[0].full_url)
            opener.open.side_effect = HTTPError('https://example.com?secret-key', 403, 'secret-key', {}, None)
            with self.assertLogs(d._LOGGER, level='WARNING') as logs, self.assertRaises(d.DartError) as raised:
                client.request('company.json')
        self.assertNotIn('secret-key', str(raised.exception))
        self.assertNotIn('secret-key', str(logs.output))
        self.assertIn('403', str(logs.output))
        with self.assertRaises(d.DartError):
            d.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_auth_ip_quota_and_maintenance_errors_are_distinct(self):
        for code in ('010', '011', '012', '020', '800', '901'):
            with self.subTest(code=code), self.assertLogs(d._LOGGER), self.assertRaises(d.DartError) as error:
                d.check_status(code, 'company.json')
            self.assertEqual(error.exception.code, code)
        d.check_status('013', 'fnlttSinglAcntAll.json')

    def test_corporation_zip_exact_mapping_cached(self):
        buffer = BytesIO()
        with ZipFile(buffer, 'w') as archive:
            archive.writestr('CORPCODE.xml', '<result><list><stock_code>005930</stock_code><corp_code>00126380</corp_code></list></result>')
        with patch.object(d.Client, 'request', return_value=buffer.getvalue()) as fetch:
            client = d.Client('key')
            self.assertEqual(client.corp_code('005930'), '00126380')
            self.assertEqual(client.corp_code('005930'), '00126380')
            with self.assertRaises(d.DartError):
                client.corp_code('005935')
            fetch.assert_called_once()

    def test_corp_api_can_return_xml_error_instead_of_zip(self):
        with patch.object(d.Client, 'request', return_value=b'<result><status>010</status><message>secret-key</message></result>'), self.assertLogs(d._LOGGER), self.assertRaises(d.DartError) as error:
            d.Client('key').corp_code('005930')
        self.assertEqual(error.exception.code, '010')
        self.assertNotIn('secret-key', str(error.exception))

    def test_cfs_priority_ofs_only_on_no_data_and_report_cache(self):
        payload = {'status': '000', 'list': [item('ifrs-full_Revenue')]}
        with patch.object(d.Client, 'json', side_effect=[{'status': '013'}, payload]) as fetch:
            client = d.Client('key')
            result = client.report('00126380', 2025, 3)
            self.assertEqual(result['scope'], 'OFS')
            client.report('00126380', 2025, 3)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual([call.kwargs['fs_div'] for call in fetch.call_args_list], ['CFS', 'OFS'])
        d._CACHE.clear()
        with patch.object(d.Client, 'json', side_effect=d.DartError('020', 'limit')) as fetch:
            with self.assertRaises(d.DartError):
                d.Client('key').report('00126380', 2025, 3)
            fetch.assert_called_once()

    def test_mismatched_report_is_rejected(self):
        with patch.object(d.Client, 'json', return_value={'status': '000', 'list': [item('ifrs-full_Revenue')]}), self.assertRaises(d.DartError) as error:
            d.Client('key').report('00126380', 2024, 3)
        self.assertEqual(error.exception.code, 'report_identity')

    def test_amount_missing_zero_negative_and_decimals(self):
        for value in ('', '-', 'NaN', 'Infinity', '1e6', None, True):
            self.assertIsNone(d.amount(value))
        self.assertEqual(d.amount('0'), 0)
        self.assertEqual(d.amount('(1,200)'), -1200)
        self.assertEqual(d.amount('12.5'), 12.5)

    def test_quarterly_income_uses_three_month_not_cumulative(self):
        row = d.statement(report(), 'income', frequency='quarterly')
        self.assertEqual(row['values']['Total Revenue'], 100)
        self.assertEqual(row['value_basis'], 'reported')
        self.assertEqual(row['period_end'], '2025-09-30')

    def test_cashflow_difference_and_fcf(self):
        prior = report(q=2)
        for row in prior['rows']:
            if row['sj_div'] == 'CF':
                row['thstrm_amount'] = str(int(row['thstrm_amount']) // 2)
        result = d.statement(report(), 'cashflow', frequency='quarterly', prior=prior)
        self.assertEqual(result['values']['Operating Cash Flow'], 40)
        self.assertEqual(result['values']['Capital Expenditure'], 13)
        self.assertEqual(result['values']['Free Cash Flow'], 27)
        self.assertEqual(result['value_basis'], 'current_ytd_minus_previous_ytd')
        self.assertEqual(len(result['source_urls']), 2)
        self.assertIsNone(d.statement(report(), 'cashflow', frequency='quarterly'))
        prior['scope'] = 'OFS'
        self.assertIsNone(d.statement(report(), 'cashflow', frequency='quarterly', prior=prior))

    def test_q4_subtracts_nine_months_not_q3_and_omits_eps(self):
        annual = report(q=4)
        annual['rows'][0]['thstrm_amount'] = '450'
        row = d.statement(annual, 'income', frequency='quarterly', prior=report())
        self.assertEqual(row['values']['Total Revenue'], 150)
        self.assertNotIn('Diluted EPS', row['values'])
        self.assertEqual(d.statement(annual, 'income', frequency='yearly')['values']['Total Revenue'], 450)

    def test_missing_capex_not_zero_and_liabilities_not_debt(self):
        current = report(q=1)
        current['rows'].pop()
        result = d.statement(current, 'cashflow', frequency='quarterly')
        self.assertNotIn('Free Cash Flow', result['values'])
        balance = d.statement(current, 'balance', frequency='quarterly')
        self.assertEqual(balance['values']['Total Liabilities'], 200)
        self.assertNotIn('Total Debt', balance['values'])

    def test_duplicate_conflict_foreign_currency_and_custom_accounts_omitted(self):
        current = report()
        current['rows'].extend([item('ifrs-full_Revenue', '999'), item('-표준계정코드 미사용-', '10000')])
        self.assertNotIn('Total Revenue', d.extract(current, 'income')[0])
        current['rows'][1]['currency'] = 'USD'
        self.assertNotIn('Operating Income', d.extract(current, 'income')[0])

    def test_all_horizon_ranges_and_metadata(self):
        def available(corp, year, quarter):
            return None if (year, quarter) == (2026, 3) else report(quarter, year)
        with patch.object(d, 'datetime', FixedDateTime), patch.object(d, 'api_key', return_value='key'), patch.object(d.Client, 'corp_code', return_value='00126380'), patch.object(d.Client, 'json', return_value={'status': '000', 'acc_mt': '12'}), patch.object(d.Client, 'report', side_effect=available):
            for horizon in ('단기', '중기', '장기'):
                with self.subTest(horizon=horizon):
                    result = d.financials(self.stock, policy(horizon))
                    self.assertEqual(result['status'], 'ok')
                    self.assertEqual(result['source'], 'OpenDART')
                    self.assertEqual(result['as_of'], '2026-06-30')
                    self.assertEqual(len(result['statements']['quarterly']['income']), policy(horizon)['quarters'])
                    self.assertEqual(len(result['statements'].get('yearly', {}).get('income', [])), policy(horizon)['annual_years'])
                    summary = financial_summary(result)
                    self.assertIn('Total Liabilities', summary['statements']['quarterly']['balance'][0]['values'])
                    self.assertEqual(summary['statements']['quarterly']['balance'][0]['scope'], 'CFS')

    def test_non_december_fiscal_year_rejected_before_financial_requests(self):
        with patch.object(d, 'api_key', return_value='key'), patch.object(d.Client, 'corp_code', return_value='00126380'), patch.object(d.Client, 'json', return_value={'status': '000', 'acc_mt': '03'}), patch.object(d.Client, 'report') as reports:
            result = d.financials(self.stock, policy('장기'))
        reports.assert_not_called()
        self.assertEqual(result['error_code'], 'fiscal_month')

    def test_deadline_prevents_further_network(self):
        client = d.Client('key')
        client.deadline = 0
        with patch.object(d, 'build_opener') as network, self.assertRaises(d.DartError) as error:
            client.request('company.json')
        network.assert_not_called()
        self.assertEqual(error.exception.code, 'deadline')

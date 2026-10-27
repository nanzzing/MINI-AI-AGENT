"""한국 공시 재무자료 어댑터. 인증키는 서버에서만 사용하며 Yahoo 형식으로 정규화한다.

연결(CFS)을 우선하고 자료 없음(013)일 때만 별도(OFS)를 시도한다.
손익의 3개월 금액과 현금흐름의 누적 금액을 구분한다. 비교 원본이 없으면
누적값을 분기값으로 대신 쓰지 않는다. 비표준 계정은 임의 추정하지 않는다.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
from threading import RLock
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from dotenv import dotenv_values

BASE = 'https://opendart.fss.or.kr/api/'
REPORTS = {1: '11013', 2: '11012', 3: '11014', 4: '11011'}
_CACHE = {}
_LOCK = RLock()
_LOGGER = logging.getLogger(__name__)

# 정확한 XBRL 계정만 매핑한다. 부채총계는 이자부채(Total Debt)가 아니다.
ACCOUNTS = {
    'income': {
        'Total Revenue': ('Revenue',),
        'Operating Income': ('dart_OperatingIncomeLoss',),
        'Net Income': ('ProfitLoss',),
        'Net Income Attributable To Parent': ('ProfitLossAttributableToOwnersOfParent',),
        'Basic EPS': ('BasicEarningsLossPerShare',),
        'Diluted EPS': ('DilutedEarningsLossPerShare',),
    },
    'balance': {
        'Total Assets': ('Assets',),
        'Total Liabilities': ('Liabilities',),
        'Total Equity': ('Equity',),
        'Stockholders Equity': ('EquityAttributableToOwnersOfParent',),
        'Cash And Cash Equivalents': ('CashAndCashEquivalents',),
    },
    'cashflow': {
        'Operating Cash Flow': ('CashFlowsFromUsedInOperatingActivities',),
        'Investing Cash Flow': ('CashFlowsFromUsedInInvestingActivities',),
        'Financing Cash Flow': ('CashFlowsFromUsedInFinancingActivities',),
        'Purchase Of PPE': ('PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities',),
        'Purchase Of Intangibles': ('PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities',),
    },
}
STATUS_MESSAGES = {
    '010': 'OpenDART API 키가 등록되지 않았습니다.',
    '011': 'OpenDART API 키 사용이 중지되었습니다.',
    '012': 'OpenDART에서 서버 IP 접근을 허용하지 않습니다.',
    '020': 'OpenDART 요청 한도를 초과했습니다.',
    '800': 'OpenDART 점검 중입니다.',
    '901': 'OpenDART API 키의 계정 상태를 확인해 주세요.',
}


class DartError(Exception):
    """외부 URL/본문이 아닌 고정된 코드와 안전한 문구만 담는다."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def api_key():
    values = dotenv_values(Path(__file__).resolve().parents[2] / '.env')
    key = os.environ.get('DART_API_KEY', values.get('DART_API_KEY') or '').strip()
    if not key or key == 'aaa':
        raise DartError('missing_key', 'DART_API_KEY 미설정: 로컬 .env 또는 Render Environment에 등록해 주세요.')
    return key


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DartError('redirect', 'OpenDART 요청 주소 변경으로 조회를 중단했습니다.')


def remember(key, ttl, loader):
    item = _CACHE.get(key)
    if item and monotonic() - item[0] < ttl:
        return deepcopy(item[1])
    result = loader()
    if len(_CACHE) >= 256:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (monotonic(), deepcopy(result))
    return result


class Client:
    def __init__(self, key):
        self.key = key
        self.deadline = monotonic() + 90

    def request(self, endpoint, **params):
        started = monotonic()
        remaining = self.deadline - started
        if remaining <= 0:
            raise DartError('deadline', 'OpenDART 전체 조회 시간이 초과되었습니다.')
        # OpenDART는 쿼리 인증 방식이다. URL·원본 예외를 로깅하지 않는다.
        request = Request(BASE + endpoint + '?' + urlencode({'crtfc_key': self.key, **params}),
                          headers={'User-Agent': 'MiniAIResearch/0.1'})
        try:
            with build_opener(NoRedirect()).open(request, timeout=min(15, remaining)) as response:
                raw = response.read(12_000_001)
            if len(raw) > 12_000_000:
                raise DartError('oversized', 'OpenDART 응답 크기 제한을 초과했습니다.')
            return raw
        except Exception as error:
            fields = dict(event='dart_request_failure', endpoint=endpoint,
                          error_type=type(error).__name__, elapsed_ms=round((monotonic() - started) * 1000))
            if isinstance(error, HTTPError):
                fields['http_status'] = error.code
                error.close()
            _LOGGER.warning('%s', json.dumps(fields))
            if isinstance(error, DartError):
                raise
            raise DartError('connection', 'OpenDART 연결 또는 응답 수신에 실패했습니다.') from None

    def json(self, endpoint, **params):
        try:
            payload = json.loads(self.request(endpoint, **params))
        except (ValueError, UnicodeError):
            raise DartError('invalid_json', 'OpenDART 응답 형식이 올바르지 않습니다.') from None
        if not isinstance(payload, dict):
            raise DartError('invalid_json', 'OpenDART 응답 형식이 올바르지 않습니다.')
        check_status(payload.get('status'), endpoint)
        return payload

    def corp_code(self, stock_code):
        def load():
            raw = self.request('corpCode.xml')
            try:
                if not raw.startswith(b'PK'):
                    root = ET.fromstring(raw)
                    check_status(root.findtext('status'), 'corpCode.xml')
                    raise ValueError('not zip')
                with ZipFile(BytesIO(raw)) as archive:
                    item = archive.getinfo('CORPCODE.xml')
                    if item.file_size > 60_000_000:
                        raise ValueError('oversized xml')
                    root = ET.fromstring(archive.read(item))
                catalog = {}
                for row in root.findall('list'):
                    stock = row.findtext('stock_code', '').strip()
                    corp = row.findtext('corp_code', '').strip()
                    if re.fullmatch(r'\d{6}', stock) and re.fullmatch(r'\d{8}', corp):
                        if stock in catalog and catalog[stock] != corp:
                            raise ValueError('ambiguous stock')
                        catalog[stock] = corp
                if not catalog:
                    raise ValueError('empty catalog')
                return catalog
            except DartError:
                raise
            except Exception:
                raise DartError('corp_format', 'OpenDART 고유번호 목록을 읽지 못했습니다.') from None
        catalog = remember(('corp_codes',), 86400, load)
        if stock_code not in catalog:
            raise DartError('unmapped_stock', 'OpenDART 종목코드 매핑이 없습니다. 우선주·종류주를 임의로 보통주에 연결하지 않습니다.')
        return catalog[stock_code]

    def report(self, corp, year, quarter):
        def load():
            for scope in ('CFS', 'OFS'):
                payload = self.json('fnlttSinglAcntAll.json', corp_code=corp, bsns_year=str(year),
                                    reprt_code=REPORTS[quarter], fs_div=scope)
                if payload['status'] == '013':
                    continue
                rows = payload.get('list')
                if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
                    raise DartError('report_format', 'OpenDART 재무제표 응답이 비어 있거나 형식이 다릅니다.')
                if any(row.get('corp_code') != corp or row.get('bsns_year') != str(year)
                       or row.get('reprt_code') != REPORTS[quarter] for row in rows):
                    raise DartError('report_identity', 'OpenDART 응답의 기업·보고기간이 요청과 다릅니다.')
                receipts = {row.get('rcept_no') for row in rows}
                if len(receipts) != 1 or not re.fullmatch(r'\d{14}', next(iter(receipts)) or ''):
                    raise DartError('receipt', 'OpenDART 공시 접수번호를 확인하지 못했습니다.')
                return dict(rows=rows, scope=scope, year=year, quarter=quarter,
                            receipt=next(iter(receipts)))
            return None
        return remember(('report', corp, year, quarter), 3600, load)


def check_status(code, endpoint):
    if code in ('000', '013'):
        return
    safe_code = code if isinstance(code, str) and re.fullmatch(r'\d{3}', code) else 'unknown'
    _LOGGER.warning('%s', json.dumps(dict(event='dart_api_failure', endpoint=endpoint, code=safe_code)))
    raise DartError(safe_code, STATUS_MESSAGES.get(safe_code, 'OpenDART 자료 조회 오류입니다.'))


def share_counts(corp, year, quarter):
    """공시 말일의 종류별 유통주식 수. 발행총수에서 자기주식을 제외했는지 검증한다."""
    with _LOCK:
        def load():
            payload = Client(api_key()).json('stockTotqySttus.json', corp_code=corp,
                                            bsns_year=str(year), reprt_code=REPORTS[quarter])
            if payload.get('status') != '000':
                raise DartError('shares_missing', 'DART 주식의 총수 현황이 없습니다.')
            counts, urls = {}, []
            total = None
            for row in payload.get('list', []):
                if row.get('corp_code') != corp or row.get('stlm_dt') != period_end(year, quarter):
                    raise DartError('shares_identity', '주식 수의 기업·기준일이 재무자료와 다릅니다.')
                label = row.get('se', '').strip()
                if label == '비고':
                    continue
                issued, treasury, outstanding = (amount(row.get(key)) for key in
                                                ('istc_totqy', 'tesstk_co', 'distb_stock_co'))
                # '-'를 0으로 치환하지 않는다. 발행총수와 유통수가 모두 있으면
                # 그 차이로 자기주식 수를 검증할 수 있다(공시에서 '-' 표기 가능).
                if treasury is None and isinstance(issued, int) and isinstance(outstanding, int):
                    treasury = issued - outstanding
                if any(not isinstance(value, int) or value < 0 for value in (issued, treasury, outstanding)) or issued - treasury != outstanding:
                    raise DartError('shares_values', '발행·자기·유통주식 수를 일치시킬 수 없습니다.')
                if label == '합계':
                    if total is not None:
                        raise DartError('shares_duplicate', '주식 수 합계가 중복됩니다.')
                    total = outstanding
                else:
                    if label in counts:
                        raise DartError('shares_duplicate', '주식 종류가 중복됩니다.')
                    counts[label] = outstanding
                receipt = row.get('rcept_no', '')
                if not re.fullmatch(r'\d{14}', receipt):
                    raise DartError('shares_receipt', '주식 수 공시 원문을 확인할 수 없습니다.')
                urls.append(f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}')
            if not counts or total is None or sum(counts.values()) != total or total <= 0:
                raise DartError('shares_total', '종류별 유통주식 수와 합계가 일치하지 않습니다.')
            return dict(counts=counts, as_of=period_end(year, quarter), source='OpenDART',
                        source_urls=sorted(set(urls)))
        return remember(('shares', corp, year, quarter), 3600, load)


def amount(raw):
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace(',', '')
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    if len(text) > 64 or not re.fullmatch(r'-?\d+(\.\d+)?', text):
        return None
    try:
        number = Decimal(text)
        return int(number) if number == number.to_integral_value() else float(number)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def account_key(account):
    for prefix in ('ifrs-full_', 'ifrs_'):
        if account.startswith(prefix):
            return account[len(prefix):]
    return account


def extract(report, kind, *, cumulative=False):
    """정확히 매핑한 KRW 계정만 사용. 중복 계정의 값이 다르면 제외한다."""
    if not report:
        return {}, {}
    rows = report['rows']
    division = {'income': 'IS', 'balance': 'BS', 'cashflow': 'CF'}[kind]
    if kind == 'income' and not any(row.get('sj_div') == 'IS' for row in rows):
        division = 'CIS'
    values, origins = {}, {}
    column = 'thstrm_add_amount' if cumulative and kind == 'income' and report['quarter'] != 4 else 'thstrm_amount'
    for name, ids in ACCOUNTS[kind].items():
        candidates = []
        for row in rows:
            if row.get('sj_div') != division or row.get('currency') != 'KRW':
                continue
            if account_key(row.get('account_id', '')) not in ids:
                continue
            value = amount(row.get(column))
            if value is not None:
                candidates.append((value, row.get('account_id'), column))
        if candidates and len({item[0] for item in candidates}) == 1:
            values[name] = candidates[0][0]
            origins[name] = {'account_id': candidates[0][1], 'column': column}
    return values, origins


def period_end(year, quarter):
    month = quarter * 3
    return date(year, month, monthrange(year, month)[1]).isoformat()


def previous_quarter(year, quarter):
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def statement(report, kind, *, frequency, prior=None):
    if not report:
        return None
    quarter = report['quarter']
    derived = frequency == 'quarterly' and ((kind == 'cashflow' and quarter > 1)
                                          or (kind == 'income' and quarter == 4))
    values, origins = extract(report, kind, cumulative=derived)
    receipts = [report['receipt']]
    if derived:
        # 비교 원본이 없으면 누적값을 단독 분기 실적으로 반환하지 않는다.
        if not prior or prior['scope'] != report['scope'] or prior['year'] != report['year'] or prior['quarter'] != quarter - 1:
            return None
        before, _ = extract(prior, kind, cumulative=True)
        # EPS는 가중평균 주식 수가 다를 수 있어 연간 EPS에서 9개월 EPS를 빼지 않는다.
        values = {key: value - before[key] for key, value in values.items() if key in before and not key.endswith('EPS')}
        origins = {key: origins[key] for key in values}
        receipts.append(prior['receipt'])
    if kind == 'cashflow' and all(name in values and values[name] >= 0 for name in ('Purchase Of PPE', 'Purchase Of Intangibles')):
        values['Capital Expenditure'] = values['Purchase Of PPE'] + values['Purchase Of Intangibles']
        if 'Operating Cash Flow' in values:
            values['Free Cash Flow'] = values['Operating Cash Flow'] - values['Capital Expenditure']
    if not values:
        return None
    return dict(period_end=period_end(report['year'], quarter), currency='KRW', values=values,
                scope=report['scope'], report_code=REPORTS[quarter], receipt_no=report['receipt'],
                source_urls=[f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}' for receipt in receipts],
                value_basis='current_ytd_minus_previous_ytd' if derived else 'reported',
                account_mapping=origins)


def financials(stock, rules):
    result = dict(source='OpenDART', status='unavailable', currency='KRW', statements={}, missing=[],
                  requested={'annual_years': rules['annual_years'], 'quarters': rules['quarters']},
                  fetched_at=datetime.now(timezone.utc).isoformat(),
                  note='DART 연결 우선·자료 없음 시 별도. KRW 기본 단위. 비표준/모호한 계정은 제외하며 누락은 0이 아닙니다.',
                  limitations=['12월 결산 기업의 주요 표준계정만 지원합니다. 결산월 변경 이력은 미검증입니다.',
                               '현금흐름 2~4분기 및 손익 4분기는 같은 연도·연결/별도 기준 누적값 차분입니다. 공시 간 정정 차이가 포함될 수 있습니다.',
                               'FCF는 영업현금흐름 - 유형·무형자산 취득 지출입니다. 두 지출을 모두 확인한 경우만 계산합니다.',
                               '부채총계는 이자부채와 다릅니다. 주식 수·선행 PER·업종 평균은 이 도구에서 제공하지 않습니다.'])
    with _LOCK:
        try:
            client = Client(api_key())
            corp = client.corp_code(stock['code'])
            company = remember(('company', corp), 86400, lambda: client.json('company.json', corp_code=corp))
            if company.get('status') != '000':
                raise DartError('company_missing', 'OpenDART 기업개황을 확인하지 못했습니다.')
            if company.get('acc_mt') != '12':
                raise DartError('fiscal_month', '12월 이외 결산 또는 결산월 미확인 기업은 기간 오인 방지를 위해 현재 지원하지 않습니다.')
            result['corp_code'] = corp
            today = datetime.now(timezone.utc).date()
            latest = (today.year, (today.month - 1) // 3 + 1)
            latest = previous_quarter(*latest)
            report = None
            # 아직 미공시인 분기는 건너뛰되 무제한 과거 탐색은 하지 않는다.
            for _ in range(4):
                report = client.report(corp, *latest)
                if report:
                    break
                latest = previous_quarter(*latest)
            if not report:
                raise DartError('no_reports', '최근 4분기에서 DART 재무제표를 찾지 못했습니다.')
            result['as_of'] = period_end(*latest)
            periods = []
            cursor = latest
            for _ in range(rules['quarters']):
                periods.append(('quarterly', *cursor))
                cursor = previous_quarter(*cursor)
            if rules['annual_years']:
                year = latest[0] if latest[1] == 4 else latest[0] - 1
                periods += [('yearly', year - offset, 4) for offset in range(rules['annual_years']) if year - offset >= 2015]
            for frequency, year, quarter in periods:
                section = result['statements'].setdefault(frequency, {name: [] for name in ACCOUNTS})
                report = client.report(corp, year, quarter)
                prior = client.report(corp, year, quarter - 1) if report and frequency == 'quarterly' and quarter > 1 else None
                for kind in ACCOUNTS:
                    row = statement(report, kind, frequency=frequency, prior=prior)
                    if row:
                        section[kind].append(row)
        except DartError as error:
            result.update(message=str(error), error_code=error.code)
        except Exception as error:
            _LOGGER.warning('%s', json.dumps(dict(event='dart_normalization_failure', error_type=type(error).__name__)))
            result.update(message='OpenDART 자료 처리에 실패했습니다.', error_code='normalization')
    for frequency, count in (('yearly', rules['annual_years']), ('quarterly', rules['quarters'])):
        if not count:
            continue
        for kind in ACCOUNTS:
            rows = result['statements'].get(frequency, {}).get(kind, [])
            if len(rows) < count:
                result['missing'].append(f'{frequency}/{kind}: 요청 {count}기간, 제공 {len(rows)}기간')
    if any(rows for section in result['statements'].values() for rows in section.values()):
        result['status'] = 'partial' if result['missing'] or result.get('error_code') else 'ok'
    elif not result.get('message'):
        result['message'] = '요청 범위에 매핑 가능한 KRW 표준 재무계정이 없습니다.'
    return result

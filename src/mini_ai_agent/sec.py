"""SEC Company Facts를 앱의 재무 형식으로 정규화한다.

표준 US-GAAP, USD, 기업 전체 자료만 사용한다. 기간별 최신 공시를 선택하고
누적 현금흐름은 같은 시작일/태그의 이전 누적값이 있을 때만 차분한다.
외국 발행인 IFRS/ADR은 통화·예탁비율 처리가 필요하므로 추정하지 않는다.
"""
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
from threading import Lock
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

_LOCK = Lock()
_LAST_REQUEST = 0.0
_LOG = logging.getLogger(__name__)
FORMS = {'10-K', '10-K/A', '10-Q', '10-Q/A'}
FIELDS = {
    'income': {
        'Total Revenue': ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'),
        'Operating Income': ('OperatingIncomeLoss',),
        'Net Income': ('NetIncomeLoss',),
        'Common Net Income': ('NetIncomeLossAvailableToCommonStockholdersBasic',),
        'Basic EPS': ('EarningsPerShareBasic',),
        'Diluted EPS': ('EarningsPerShareDiluted',),
    },
    'balance': {
        'Total Assets': ('Assets',), 'Total Liabilities': ('Liabilities',),
        'Stockholders Equity': ('StockholdersEquity',),
        'Cash And Cash Equivalents': ('CashAndCashEquivalentsAtCarryingValue',),
        'Ordinary Shares Number': ('CommonStockSharesOutstanding',),
        'Preferred Stock': ('PreferredStockValue', 'PreferredStockCarryingValue'),
    },
    'cashflow': {
        'Operating Cash Flow': ('NetCashProvidedByUsedInOperatingActivities',),
        'Purchase Of PPE': ('PaymentsToAcquirePropertyPlantAndEquipment',),
        'Investing Cash Flow': ('NetCashProvidedByUsedInInvestingActivities',),
        'Financing Cash Flow': ('NetCashProvidedByUsedInFinancingActivities',),
    },
}


class SecError(Exception):
    pass


def user_agent():
    values = dotenv_values(Path(__file__).resolve().parents[2] / '.env')
    value = os.environ.get('SEC_USER_AGENT', values.get('SEC_USER_AGENT', '')) or ''
    if not re.fullmatch(r'[^\r\n]{3,200}', value) or not re.search(r'[^\s@]+@[^\s@]+\.[^\s@]+', value) or 'example.com' in value:
        raise SecError('SEC_USER_AGENT에 서비스 이름과 실제 연락 이메일을 설정해 주세요.')
    return value


def fetch_json(url):
    """서버에서만 조회. 프로세스 전체 요청 간격을 제한하고 본문/이메일은 로그에 남기지 않는다."""
    global _LAST_REQUEST
    headers = {'User-Agent': user_agent(), 'Accept': 'application/json'}
    started = monotonic()
    try:
        with _LOCK:
            sleep(max(0, .2 - (monotonic() - _LAST_REQUEST)))
            _LAST_REQUEST = monotonic()
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            raw = response.read(25_000_001)
        if len(raw) > 25_000_000:
            raise ValueError('response size')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('response shape')
        return result
    except Exception as error:
        code = error.code if isinstance(error, HTTPError) else None
        _LOG.warning(json.dumps({'event': 'sec_failure', 'error_type': type(error).__name__,
                                'http_status': code, 'elapsed_ms': round((monotonic() - started) * 1000)}))
        raise SecError(f'SEC 조회 실패 ({code or type(error).__name__}). Yahoo 재무로 대체하지 않습니다.') from None


def company(stock):
    from mini_ai_agent.market_data import cached
    def directory():
        data = fetch_json('https://www.sec.gov/files/company_tickers_exchange.json')
        return {'status': 'ok', 'rows': [dict(zip(data['fields'], row)) for row in data['data']]}
    rows = cached(('sec', 'directory'), 86400, directory)['rows']
    normalize = lambda value: str(value).upper().replace('.', '-').replace('/', '-')
    exchange = {'NASDAQ': 'NASDAQ', 'NYSE': 'NYSE', 'AMEX': 'NYSE', 'NYSEAMERICAN': 'NYSE'}
    matches = [row for row in rows if normalize(row['ticker']) == normalize(stock['code'])
               and str(row['exchange']).upper() == exchange.get(stock['exchange'].upper(), stock['exchange'].upper())]
    if len(matches) != 1:
        raise SecError('SEC에서 종목과 거래소에 대응하는 기업을 하나로 확인하지 못했습니다.')
    cik = str(int(matches[0]['cik'])).zfill(10)
    tickers = sorted({row['ticker'] for row in rows if str(row['cik']) == str(matches[0]['cik'])})
    return cik, tickers


def valid_fact(item, unit):
    try:
        end, filed = date.fromisoformat(item['end']), date.fromisoformat(item['filed'])
        today = datetime.now(timezone.utc).date()
        value = item['val']
        return (item.get('form') in FORMS and end <= filed <= today
                and re.fullmatch(r'\d{10}-\d{2}-\d{6}', item.get('accn', ''))
                and not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value))
    except (KeyError, ValueError, TypeError):
        return False


def facts_for(data, tags, unit):
    """다른 태그끼리 차분하지 않는다. 동일 기간에서는 우선 태그를 사용한다."""
    result = []
    for priority, tag in enumerate(tags):
        for item in data.get('facts', {}).get('us-gaap', {}).get(tag, {}).get('units', {}).get(unit, []):
            if valid_fact(item, unit):
                result.append({**item, 'tag': tag, 'priority': priority})
    # 같은 태그·시작일·종료일의 재공시/정정은 최신 제출을 채택한다.
    selected = {}
    for item in result:
        key = (item['tag'], item.get('start'), item['end'])
        if key not in selected or (item['filed'], item['accn']) > (selected[key]['filed'], selected[key]['accn']):
            selected[key] = item
    return list(selected.values())


def duration(item):
    try:
        return (date.fromisoformat(item['end']) - date.fromisoformat(item['start'])).days + 1
    except (KeyError, ValueError):
        return 0


def normalized_values(items, frequency, instant=False, additive=True):
    candidates = []
    for item in items:
        days = duration(item)
        if instant:
            if frequency == 'yearly' and item['form'] not in {'10-K', '10-K/A'}:
                continue
            candidates.append({**item, 'parts': [item], 'basis': 'instant'})
        elif (frequency == 'yearly' and 330 <= days <= 380) or (frequency == 'quarterly' and 70 <= days <= 110):
            candidates.append({**item, 'parts': [item], 'basis': 'reported'})
        elif frequency == 'quarterly' and additive and 140 <= days <= 380:
            previous = [p for p in items if p['tag'] == item['tag'] and p.get('start') == item.get('start')
                        and 70 <= (date.fromisoformat(item['end']) - date.fromisoformat(p['end'])).days <= 110
                        and duration(p) >= 70]
            if previous:
                p = max(previous, key=lambda row: (row['end'], row['filed']))
                candidates.append({**item, 'start': (date.fromisoformat(p['end']) + timedelta(days=1)).isoformat(),
                                   'val': item['val'] - p['val'], 'parts': [item, p], 'basis': 'cumulative_difference'})
    chosen = {}
    for item in candidates:
        # 최신 공시를 우선하고 같은 날이면 태그 우선도와 직접 보고값으로 결정한다.
        key = item['end']
        rank = (item['filed'], -item['priority'], item['basis'] != 'cumulative_difference')
        old = chosen.get(key)
        if old is None or rank > old[0]:
            chosen[key] = (rank, item)
    return {end: row for end, (_, row) in chosen.items()}


def normalize(data, cik, tickers, rules):
    from mini_ai_agent.market_data import meta
    statements, missing = {}, []
    for frequency, count in [('yearly', rules['annual_years']), ('quarterly', rules['quarters'])]:
        if not count:
            continue
        section = statements[frequency] = {}
        for kind, fields in FIELDS.items():
            by_end = {}
            for name, tags in fields.items():
                unit = 'shares' if name == 'Ordinary Shares Number' else 'USD/shares' if 'EPS' in name else 'USD'
                items = normalized_values(facts_for(data, tags, unit), frequency, kind == 'balance', 'EPS' not in name)
                for end, item in items.items():
                    row = by_end.setdefault(end, {'period_end': end, 'currency': 'USD', 'scope': 'CFS',
                                                 'values': {}, 'field_metadata': {}, 'source_urls': []})
                    urls = [f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{p['accn'].replace('-', '')}/" for p in item['parts']]
                    row['values'][name] = item['val']
                    row['field_metadata'][name] = {'start': item.get('start'), 'end': end, 'tag': item['tag'],
                                                   'filed': item['filed'], 'basis': item['basis'], 'source_urls': urls}
                    row['source_urls'] = sorted(set(row['source_urls'] + urls))
            # 같은 기간의 영업CF와 유형자산 취득으로만 FCF를 계산한다.
            if kind == 'cashflow':
                for row in by_end.values():
                    fields_meta, values = row['field_metadata'], row['values']
                    if all(k in values for k in ('Operating Cash Flow', 'Purchase Of PPE')):
                        if fields_meta['Operating Cash Flow']['start'] == fields_meta['Purchase Of PPE']['start'] and values['Purchase Of PPE'] >= 0:
                            values['Capital Expenditure'] = -values['Purchase Of PPE']
                            values['Free Cash Flow'] = values['Operating Cash Flow'] - values['Purchase Of PPE']
                            fields_meta['Free Cash Flow'] = {**fields_meta['Operating Cash Flow'], 'basis': 'OCF minus PPE',
                                                            'source_urls': row['source_urls']}
            if kind == 'balance':
                # 표지의 주식 수 날짜나 연간 공시의 비교 분기를 회계 말일로 혼용하지 않는다.
                ends = {row['period_end'] for row in section.get('income', [])}
                by_end = {end: row for end, row in by_end.items() if end in ends}
            section[kind] = sorted(by_end.values(), key=lambda row: row['period_end'], reverse=True)[:count]
            if len(section[kind]) < count:
                missing.append(f'{frequency}/{kind}: 요청 {count}기간, 제공 {len(section[kind])}기간')
            for row in section[kind]:
                absent = set(fields) - row['values'].keys()
                if absent:
                    missing.append(f"{frequency}/{kind}/{row['period_end']}: " + ', '.join(sorted(absent)))
    dates = [row['period_end'] for section in statements.values() for rows in section.values() for row in rows]
    return meta('SEC EDGAR', 'partial' if dates else 'unavailable', statements=statements, missing=missing,
                currency='USD', cik=cik, tickers=tickers, as_of=max(dates) if dates else None,
                requested={'annual_years': rules['annual_years'], 'quarters': rules['quarters']},
                note='US-GAAP 표준 태그·USD 기업 전체 공시. 미제공 항목은 0이 아님. FCF=영업CF-유형자산 취득; 인수·무형자산 취득 제외. '
                     '누적 차분에 정정 기준 차이가 남을 수 있음. EPS는 누적 차분하지 않음. IFRS/외국 발행인·사업부 자료 미지원.')


def financials(stock, rules):
    from mini_ai_agent.market_data import cached, meta
    try:
        user_agent()
        cik, tickers = company(stock)
        def load():
            data = fetch_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json')
            if str(int(data.get('cik', 0))).zfill(10) != cik:
                raise SecError('SEC 기업 식별자가 일치하지 않습니다.')
            return {'status': 'ok', 'data': data}
        data = cached(('sec', 'companyfacts', cik), 3600, load)['data']
        return normalize(data, cik, tickers, rules)
    except SecError as error:
        return meta('SEC EDGAR', 'unavailable', statements={}, missing=['SEC 재무 조회 실패'], message=str(error))
    except Exception:
        return meta('SEC EDGAR', 'unavailable', statements={}, missing=['SEC 응답 형식 확인 필요'], message='SEC 응답을 처리하지 못했습니다.')

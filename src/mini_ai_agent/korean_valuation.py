"""DART 실적과 별도 일봉 종가로 계산하는 한국 기업 전체 기준 가치평가.

주식 수는 공시 말일 값이므로 현재 시가총액의 추정치다. 종류주를 보통주
가격으로 평가하지 않으며, 검증된 코드 매핑이 없는 종류주는 계산을 막는다.
"""
from datetime import date, datetime, timezone, timedelta
import json
import math
from pathlib import Path

import yfinance as yf

from mini_ai_agent import dart
from mini_ai_agent.market_data import financials, meta, yahoo_symbol


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return value if math.isfinite(value) else None


def row_at(rows, end):
    matches = [row for row in rows if row.get('period_end') == end]
    return matches[0] if len(matches) == 1 else None


def same_basis(*rows):
    return bool(rows) and all(row and row.get('currency') == 'KRW' for row in rows) and len({row.get('scope') for row in rows}) == 1 and rows[0].get('scope') in {'CFS', 'OFS'}


def parent_value(row, parent, standalone):
    # 연결 당기순이익/전체 자본에는 비지배지분이 포함되므로 대신 쓰지 않는다.
    key = parent if row.get('scope') == 'CFS' else standalone
    return numeric(row.get('values', {}).get(key))


def ttm_income(data):
    """최신 공시 분기부터 연속 4개 단독 분기만 사용한다. 누락 시 과거로 밀지 않는다."""
    quarterly = data.get('statements', {}).get('quarterly', {})
    incomes = quarterly.get('income', [])
    ends = [row.get('period_end') for kind in ('income', 'balance') for row in quarterly.get(kind, [])]
    ends.append(data.get('as_of'))
    ends = [end for end in ends if isinstance(end, str) and end]
    if not ends:
        return (), None, '', '최신 분기 기준일을 확인할 수 없습니다.'
    try:
        latest = date.fromisoformat(max(ends))
        quarter = (latest.month - 1) // 3 + 1
        if latest.isoformat() != dart.period_end(latest.year, quarter):
            raise ValueError
    except ValueError:
        return (), None, '', '정상적인 분기 말일을 확인할 수 없습니다.'
    cursor = (latest.year, quarter)
    expected = []
    for _ in range(4):
        expected.append(dart.period_end(*cursor))
        cursor = dart.previous_quarter(*cursor)
    start = date.fromisoformat(dart.period_end(*cursor)) + timedelta(days=1)
    period = f'{start.isoformat()} ~ {latest.isoformat()} (TTM, 최근 4개 분기)'
    rows = tuple(row_at(incomes, end) for end in expected)
    if not all(rows):
        return tuple(row for row in rows if row), None, period, '최신 연속 4개 분기 손익이 없거나 중복됩니다. 연간 PER로 대체하지 않습니다.'
    if not same_basis(*rows):
        return rows, None, period, 'TTM 4개 분기의 통화 또는 연결·별도 기준이 다릅니다.'
    values = [parent_value(row, 'Net Income Attributable To Parent', 'Net Income') for row in rows]
    if any(value is None for value in values):
        return rows, None, period, 'TTM 분기별 지배주주 순이익이 누락되었습니다. 전체 순이익으로 대체하지 않습니다.'
    # DART 어댑터가 Q4를 연간-9개월 누적으로 이미 차분했다. 여기서 재차분하지 않는다.
    return rows, sum(values), period, ''


def close_price(stock, shares_date):
    """get_info 없이 가격 차트 API만 사용. 배당 조정 종가를 배수 계산에 쓰지 않는다."""
    today = datetime.now(timezone.utc).date()
    start = date.fromisoformat(shares_date)
    if not 0 <= (today - start).days <= 550:
        raise ValueError('주식 수 기준일이 미래이거나 550일 이상 오래됐습니다.')
    ticker = yf.Ticker(yahoo_symbol(stock))
    frame = ticker.history(start=(start + timedelta(days=1)).isoformat(), interval='1d',
                           auto_adjust=False, actions=True, timeout=15)
    if frame.empty or ticker.history_metadata.get('currency') != 'KRW':
        raise ValueError('KRW 종가를 확인하지 못했습니다.')
    # yfinance의 비조정 종가도 과거 분할을 반영할 수 있다. 공시 이후 분할 시
    # 과거 주식 수와 결합하지 않고 새 공시를 기다린다.
    if 'Stock Splits' not in frame or any(float(value) not in (0, 1) for value in frame['Stock Splits']):
        raise ValueError('공시 이후 주식분할 또는 병합 여부 확인이 필요합니다.')
    end = frame.index[-1].date()
    price = float(frame['Close'].iloc[-1])
    if not math.isfinite(price) or price <= 0 or not 0 <= (today - end).days <= 14:
        raise ValueError('유효한 최근 14일 이내 종가가 없습니다.')
    return {'value': price, 'as_of': end.isoformat(), 'source': 'Yahoo Finance chart',
            'adjustment': 'auto_adjust=False; 배당 비조정 최근 종가',
            'url': f'https://finance.yahoo.com/quote/{yahoo_symbol(stock)}/history/'}


def equity_market_value(stock, shares):
    mapping = json.loads((Path(__file__).parent / 'data' / 'kr_share_classes.json').read_text(encoding='utf-8'))
    entry = mapping.get(f"{stock['exchange']}:{stock['code']}")
    active = {label: count for label, count in shares['counts'].items() if count > 0}
    # 보통주만 발행한 기업은 DART의 정확한 종목코드 매핑을 그대로 사용한다.
    codes = entry['classes'] if entry else {'보통주': stock['code']}
    if set(active) - set(codes) or not active or len(set(codes.values())) != len(codes):
        raise ValueError('종류주 전체의 검증된 종목코드 매핑이 없어 기업 시가총액을 계산할 수 없습니다.')
    components = []
    for label, count in active.items():
        quote = close_price({**stock, 'code': codes[label]}, shares['as_of'])
        components.append(dict(share_class=label, code=codes[label], outstanding=count, price=quote))
    if len({part['price']['as_of'] for part in components}) != 1:
        raise ValueError('종류별 주가 기준일이 서로 다릅니다.')
    return dict(value=sum(part['outstanding'] * part['price']['value'] for part in components),
                as_of=components[0]['price']['as_of'], shares_as_of=shares['as_of'],
                components=components, source_urls=shares['source_urls'],
                mapping_source=entry['source'] if entry else 'OpenDART corpCode + stockTotqySttus',
                estimated=True)


def calculate(stock):
    data = financials(stock, '장기')
    statements = data.get('statements', {})
    annual = statements.get('yearly', {})
    metrics = {}

    def put(key, value=None, *, period='', method='', inputs=None, rows=(), reason='', unit='배', estimated=False):
        value = numeric(value)
        metrics[key] = dict(value=value, status='ok' if value is not None else 'unavailable',
                            period=period, unit=unit, method=method, inputs=inputs or {}, estimated=estimated,
                            scope=rows[0].get('scope') if rows else None,
                            source_urls=sorted({url for row in rows for url in row.get('source_urls', [])}),
                            reason='' if value is not None else reason)

    ttm_rows, ttm_profit, ttm_period, ttm_reason = ttm_income(data)
    # TTM 이익과 정확히 같은 기간의 양 끝 자본을 비교한다. 전년도 연간
    # 자본으로 임의 대체하지 않으며, 12월 말만 연간 재무상태표를 보완 사용한다.
    def balance_at(end):
        quarterly = statements.get('quarterly', {}).get('balance', [])
        matches = [row for row in quarterly if row.get('period_end') == end]
        if matches:
            return matches[0] if len(matches) == 1 else None
        return row_at(annual.get('balance', []), end) if end.endswith('-12-31') else None

    end_date = ttm_rows[0]['period_end'] if ttm_profit is not None else ''
    start_date = f'{int(end_date[:4]) - 1}{end_date[4:]}' if end_date else ''
    closing = balance_at(end_date) if end_date else None
    previous = balance_at(start_date) if start_date else None
    end_equity = parent_value(closing, 'Stockholders Equity', 'Total Equity') if closing else None
    start_equity = parent_value(previous, 'Stockholders Equity', 'Total Equity') if previous else None
    roe_ok = same_basis(*ttm_rows, closing, previous) and ttm_profit is not None and all(value is not None and value > 0 for value in (start_equity, end_equity))
    put('roe', ttm_profit / ((start_equity + end_equity) / 2) * 100 if roe_ok else None,
        period=ttm_period, unit='%',
        method='최근 4분기 지배주주 순이익 합 / 기간 시작·끝 지배주주 자본 평균 * 100 (별도는 당기순이익·전체 자본)',
        inputs={'net_income': ttm_profit, 'opening_equity': start_equity, 'closing_equity': end_equity,
                'opening_equity_as_of': start_date, 'closing_equity_as_of': end_date,
                'quarters': [{'period_end': row['period_end'],
                              'net_income': parent_value(row, 'Net Income Attributable To Parent', 'Net Income'),
                              'value_basis': row.get('value_basis')} for row in ttm_rows]},
        rows=ttm_rows + tuple(row for row in (closing, previous) if row),
        reason=ttm_reason or '동일 통화·연결/별도 기준 TTM 순이익과 해당 기간 시작·끝의 양의 자본이 필요합니다.')

    balances = statements.get('quarterly', {}).get('balance', []) or annual.get('balance', [])
    balance = max(balances, key=lambda row: row['period_end']) if balances else None
    cap, cap_reason = None, data.get('message') or '주식 수 기준 재무상태표가 없습니다.'
    if balance and data.get('corp_code') and balance.get('currency') == 'KRW':
        try:
            end = date.fromisoformat(balance['period_end'])
            shares = dart.share_counts(data['corp_code'], end.year, end.month // 3)
            cap = equity_market_value(stock, shares)
        except dart.DartError as error:
            cap_reason = str(error)
        except ValueError as error:
            cap_reason = str(error)
        except Exception:
            cap_reason = '주가·주식 수 조회에 실패했습니다. Yahoo 가치평가 지표로 대체하지 않습니다.'
    cap_value = cap['value'] if cap else None
    pe_ok = cap is not None and same_basis(*ttm_rows, balance) and ttm_profit is not None and ttm_profit > 0
    put('trailing_pe', cap_value / ttm_profit if pe_ok else None,
        period=ttm_period,
        method='종류별 종가 × 공시 유통주식 수의 합 / 최근 연속 4분기 지배주주 순이익 합 (별도는 당기순이익)',
        inputs={'estimated_equity_market_value': cap_value, 'net_income': ttm_profit,
                'quarters': [{'period_end': row['period_end'],
                              'net_income': parent_value(row, 'Net Income Attributable To Parent', 'Net Income'),
                              'scope': row.get('scope'), 'value_basis': row.get('value_basis'),
                              'source_urls': row.get('source_urls', [])} for row in ttm_rows]},
        rows=ttm_rows, estimated=True,
        reason=ttm_reason or (cap_reason if not cap else '동일 연결/별도 기준의 양의 TTM 순이익이 필요합니다. TTM 적자·0이면 PER 비교 불가.'))
    book = parent_value(balance, 'Stockholders Equity', 'Total Equity') if balance else None
    pb_ok = cap is not None and book is not None and book > 0 and balance.get('scope') in {'CFS', 'OFS'}
    put('price_to_book', cap_value / book if pb_ok else None,
        period=balance['period_end'] if balance else '', method='종류별 시가총액 추정치 합 / 지배주주 자본 (별도는 전체 자본)',
        inputs={'estimated_equity_market_value': cap_value, 'equity': book},
        rows=(balance,) if balance else (), estimated=True,
        reason=cap_reason if not cap else '양의 지배주주 자본이 필요합니다. 비지배지분 포함 자본으로 대체하지 않습니다.')
    for key in ('trailing_pe', 'price_to_book'):
        metrics[key]['inputs']['market_value_basis'] = cap
        metrics[key]['price_as_of'] = cap['as_of'] if cap else None
        if cap:
            metrics[key]['source_urls'] = sorted(set(metrics[key]['source_urls'] + cap['source_urls'] +
                                                   [part['price']['url'] for part in cap['components']]))
    # 연결 FCF에는 비지배지분 현금흐름이 포함될 수 있으므로 주주 귀속
    # 현금흐름으로 오해할 수 있는 FCF 수익률은 이번 계산 범위에서 제외한다.
    for key, reason in {
        'forward_pe': 'DART는 미래 예상 EPS 컨센서스를 제공하지 않습니다. 선행 PER 별도 공급원 미연결.',
        'ev_to_ebitda': '이자부채·비지배지분·EBITDA의 동일 기준 자료가 없어 계산하지 않습니다.',
        'fcf_yield': '전체 주주가치와 비교 가능한 FCF 귀속 범위 검증 미구현. DART 현금흐름 자체는 재무 도구에서 제공합니다.',
    }.items():
        put(key, reason=reason, unit='%' if key == 'fcf_yield' else '배')
    missing = [key for key, value in metrics.items() if value['status'] != 'ok']
    return meta('OpenDART + Yahoo Finance chart (서버 직접 계산)',
                'partial' if any(value['status'] == 'ok' for value in metrics.values()) else 'unavailable',
                stock=stock, currency='KRW', financial_currency='KRW', metrics=metrics,
                as_of=balance['period_end'] if balance else None,
                price_as_of=cap['as_of'] if cap else None, market_value=cap,
                benchmark={'status': 'unavailable', 'reason': '동종업계 동일 기준 비교 공급원 미연결'},
                historical={'status': 'unavailable', 'reason': '과거 시점별 가치평가 미구현'},
                missing=missing + ['industry_benchmark', 'historical_valuation'],
                limitations=['PER·PBR은 보통주 단독 배수가 아니라 종류주를 모두 포함한 기업 전체 기준 추정 배수입니다.',
                             'PER·ROE는 최신 공시부터 연속 4개 단독 분기의 TTM 기준입니다. PBR은 최신 분기 말 자본 기준이며 선행 지표는 제공하지 않습니다.',
                             'TTM의 4분기 손익은 DART 연간-9개월 누적 차분을 포함할 수 있습니다. 공시 간 정정 차이가 남아 있을 수 있습니다.',
                             '공시 말일 유통주식 수를 최근 종가에 적용합니다. 이후 증자·소각·자기주식 변동은 미반영이며 현재 시가총액 확정값이 아닙니다.',
                             '공시 이후 분할·병합, 종류주 매핑 누락, 가격 기준일 불일치는 계산을 중단합니다.',
                             'ROE는 지배주주 순이익 / 평균 지배주주 자본입니다. 별도 공시에서는 당기순이익 / 평균 전체 자본입니다.',
                             '서로 다른 공시에서 가져온 기초·기말 자본은 정정 공시의 재작성 기준 차이가 있을 수 있습니다.',
                             'Yahoo get_info의 재무·가치평가 수치는 한국 종목에 사용하지 않습니다. 가격 API 장애는 별도로 발생할 수 있습니다.'])

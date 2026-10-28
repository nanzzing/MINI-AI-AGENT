"""SEC 공시 실적과 Yahoo 가격만으로 미국 기업 배수를 계산한다.

예상 EPS나 Yahoo get_info 배수로 조용히 대체하지 않는다. 공시 주식 수를
사용하므로 시가총액은 추정치이며, 복수 상장 종류주/분할 발생 시 계산을 막는다.
"""
from datetime import date, datetime, timedelta, timezone
import math

import yfinance as yf

from mini_ai_agent.market_data import financials, meta, yahoo_symbol


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def ttm(data, kind, field):
    rows = data.get('statements', {}).get('quarterly', {}).get(kind, [])[:4]
    if len(rows) != 4 or rows[0]['period_end'] != data.get('as_of'):
        return None, (), ''
    spans, values = [], []
    for row in reversed(rows):
        details = row.get('field_metadata', {}).get(field, {})
        value = number(row['values'].get(field))
        try:
            start, end = date.fromisoformat(details['start']), date.fromisoformat(row['period_end'])
        except (KeyError, ValueError, TypeError):
            return None, (), ''
        if value is None or row.get('currency') != 'USD' or not 70 <= (end - start).days + 1 <= 110:
            return None, (), ''
        if spans and start != spans[-1][1] + timedelta(days=1):
            return None, (), ''
        spans.append((start, end))
        values.append(value)
    if not 330 <= (spans[-1][1] - spans[0][0]).days + 1 <= 380:
        return None, (), ''
    return sum(values), tuple(rows), f'{spans[0][0]} ~ {spans[-1][1]} (TTM)'


def market_value(stock, data, balance):
    if len(data.get('tickers', [])) != 1:
        raise ValueError('복수 종류주/티커의 시가총액 합산 기준 미검증')
    shares = number(balance['values'].get('Ordinary Shares Number'))
    if shares is None or shares <= 0:
        raise ValueError('기업 전체 보통주 수 미제공')
    today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(balance['period_end'])
    if not 0 <= (today - end).days <= 550:
        raise ValueError('공시 주식 수 기준일이 미래이거나 오래됨')
    ticker = yf.Ticker(yahoo_symbol(stock))
    frame = ticker.history(start=(end + timedelta(days=1)).isoformat(), interval='1d',
                           auto_adjust=False, actions=True, timeout=15)
    if frame.empty or ticker.history_metadata.get('currency') != 'USD':
        raise ValueError('USD 최근 종가 확인 불가')
    if 'Stock Splits' not in frame or any(float(v) not in (0, 1) for v in frame['Stock Splits']):
        raise ValueError('공시 이후 주식분할·병합 확인 필요')
    price = number(float(frame['Close'].iloc[-1]))
    stamp = frame.index[-1].date()
    if price is None or price <= 0 or not 0 <= (today - stamp).days <= 14:
        raise ValueError('최근 14일 이내 유효한 종가 없음')
    return {'value': shares * price, 'shares': shares, 'shares_as_of': end.isoformat(),
            'price': price, 'as_of': stamp.isoformat(), 'estimated': True,
            'source_urls': balance.get('source_urls', []) + [f'https://finance.yahoo.com/quote/{yahoo_symbol(stock)}/history/']}


def calculate(stock):
    data = financials(stock, '장기')
    quarterly = data.get('statements', {}).get('quarterly', {})
    balances = quarterly.get('balance', [])
    balance = next((r for r in balances if r['period_end'] == data.get('as_of')), None)
    metrics = {}
    profit_field = 'Common Net Income'
    profit, rows, period = ttm(data, 'income', profit_field)
    # NetIncomeLoss는 지배기업 귀속이지만 우선주 배당 차감 전일 수 있다.
    # common 순이익이 없을 때 우선주 잔액 0이 명시된 경우만 대체한다.
    income_ends = [r['period_end'] for r in quarterly.get('income', [])[:4]]
    no_preferred = len(income_ends) == 4 and all(
        any(b['period_end'] == end and b['values'].get('Preferred Stock') == 0 for b in balances)
        for end in income_ends)
    if profit is None and no_preferred:
        profit_field = 'Net Income'
        profit, rows, period = ttm(data, 'income', profit_field)
    cap, cap_error = None, data.get('message') or '동일 최신 분기의 주식 수·자본이 없습니다.'
    if balance:
        try:
            cap = market_value(stock, data, balance)
        except ValueError as error:
            cap_error = str(error)
        except Exception:
            cap_error = '가격 조회 실패. Yahoo 재무 배수로 대체하지 않습니다.'
    value = cap['value'] if cap else None
    def put(key, amount=None, *, method='', inputs=None, source_rows=(), reason='', unit='배', period_value=period):
        amount = number(amount)
        urls = {url for row in source_rows for url in row.get('source_urls', [])}
        if cap and key != 'roe':
            urls.update(cap['source_urls'])
        metrics[key] = {'value': amount, 'status': 'ok' if amount is not None else 'unavailable',
                        'unit': unit, 'method': method, 'inputs': inputs or {}, 'period': period_value,
                        'source_urls': sorted(urls), 'reason': '' if amount is not None else reason,
                        'price_as_of': cap['as_of'] if cap and key != 'roe' else None}
    put('trailing_pe', value / profit if cap and profit is not None and profit > 0 else None,
        method='공시 보통주 수 × 최근 종가 / TTM 보통주 귀속 순이익',
        inputs={'market_value': cap, 'ttm_common_net_income': profit}, source_rows=rows,
        reason=cap_error if not cap else '최신 연속 4분기의 양의 보통주 귀속 순이익 미확인')
    if profit is None and cap:
        # 보통주 귀속 순이익 미공시 시 직접 보고된 4분기 희석 EPS로만 보완한다.
        # Q4 EPS를 연간-9개월로 차분하지 않으므로 많은 기업에서 미확인으로 남을 수 있다.
        eps, eps_rows, eps_period = ttm(data, 'income', 'Diluted EPS')
        if eps is not None and eps > 0:
            put('trailing_pe', cap['price'] / eps, period_value=eps_period,
                method='최근 종가 / 직접 보고된 최근 4분기 희석 EPS 합 (분기별 가중평균 주식 수 차이 존재)',
                inputs={'price': cap['price'], 'ttm_diluted_eps_sum': eps}, source_rows=eps_rows)
    equity = number(balance['values'].get('Stockholders Equity')) if balance else None
    preferred = number(balance['values'].get('Preferred Stock')) if balance else None
    # 우선주 존재 여부가 불명확하면 전체 지배주주 자본을 보통주 자본으로 간주하지 않는다.
    book = equity if preferred == 0 else None
    put('price_to_book', value / book if cap and book is not None and book > 0 else None,
        method='시가총액 추정치 / 최신 분기 보통주 자본 (우선주 잔액 0 확인)',
        inputs={'market_value': cap, 'equity': book}, source_rows=(balance,) if balance else (),
        period_value=balance['period_end'] if balance else '', reason=cap_error if not cap else '양의 보통주 자본·우선주 잔액 미확인')
    opening = None
    if rows:
        # 52/53주 회계연도도 지원: TTM 시작 전날의 실제 재무상태표를 찾는다.
        start = min(v['field_metadata'][profit_field]['start'] for v in rows)
        opening_end = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        opening = next((r for r in balances if r['period_end'] == opening_end), None)
    opening_equity = number(opening['values'].get('Stockholders Equity')) if opening and opening['values'].get('Preferred Stock') == 0 else None
    roe_ok = profit is not None and book is not None and book > 0 and opening_equity is not None and opening_equity > 0
    put('roe', profit / ((book + opening_equity) / 2) * 100 if roe_ok else None,
        method='TTM 보통주 귀속 순이익 / 기초·기말 보통주 자본 평균 × 100', unit='%',
        inputs={'net_income': profit, 'opening_equity': opening_equity, 'closing_equity': book},
        source_rows=rows + tuple(r for r in (opening, balance) if r), reason='TTM 순이익 또는 동일 기간 양 끝 보통주 자본 미확인')
    fcf, cf_rows, cf_period = ttm(data, 'cashflow', 'Free Cash Flow')
    put('fcf_yield', fcf / value * 100 if cap and fcf is not None else None, unit='%', period_value=cf_period,
        method='TTM (영업현금흐름 - 유형자산 취득) / 시가총액 추정치 × 100; 기업 전체 CF 보조지표',
        inputs={'ttm_fcf': fcf, 'market_value': cap}, source_rows=cf_rows,
        reason=cap_error if not cap else '최신 연속 4분기 동일 기간 현금흐름 미확인')
    for key, reason in [('forward_pe', 'SEC 공시는 시장 예상 EPS가 아닙니다. 예상치 공급원 미연결.'),
                        ('ev_to_ebitda', '동일 기준 이자부채·우선주·비지배지분·EBITDA 검증 미구현.')]:
        put(key, reason=reason, period_value='')
    return meta('SEC EDGAR + Yahoo Finance chart (서버 직접 계산)',
                'partial' if any(m['status'] == 'ok' for m in metrics.values()) else 'unavailable',
                stock=stock, metrics=metrics, currency='USD', financial_currency='USD',
                as_of=data.get('as_of'), price_as_of=cap['as_of'] if cap else None, market_value=cap,
                benchmark={'status': 'unavailable', 'reason': '동종업계 비교 공급원 미연결'},
                historical={'status': 'unavailable', 'reason': '과거 시점별 배수 미구현'},
                missing=[k for k, m in metrics.items() if m['status'] != 'ok'] + ['industry_benchmark', 'historical_valuation'],
                limitations=['공시 주식 수 이후 증자·소각·자사주 변화는 미반영. 현재 시가총액 확정값이 아닙니다.',
                             '복수 티커·종류주, 공시 이후 분할, 비USD/IFRS/ADR은 추정하지 않습니다.',
                             'FCF는 기업 전체 영업CF-유형자산 취득이며 인수·무형자산 취득 및 비지배지분 귀속 조정은 제외합니다.',
                             '누적 차분과 기초·기말 자본에 공시 간 정정 기준 차이가 남을 수 있습니다.',
                             '주가 기준일과 재무 기준일은 다릅니다. 선행 PER·업종 평균은 미제공입니다.'])

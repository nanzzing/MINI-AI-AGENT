"""장기 가치평가 스냅샷. 기업 값과 아직 확보하지 못한 비교 자료를 분리한다."""
from datetime import datetime, timezone
import math

import yfinance as yf

from mini_ai_agent.market_data import cached, meta, yahoo_symbol


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def valuation_comparison(stock):
    """한국·미국 모두 공급자 배수를 사용한다. DART/SEC 재무 조회는 별개다."""
    return yahoo_valuation_snapshot(stock)


def yahoo_valuation_snapshot(stock):
    """공급자 원본 지표만 사용하며 누락값을 공시 자료로 재계산하지 않는다."""

    def fetch():
        symbol = yahoo_symbol(stock)
        try:
            info = yf.Ticker(symbol).get_info()
            if not isinstance(info, dict):
                raise ValueError
        except Exception:
            return meta('Yahoo Finance', 'unavailable', metrics={},
                        message='Yahoo 가치평가 조회 실패. 접근 제한·인증 또는 네트워크 문제일 수 있습니다. DART/SEC 재무 조회와는 별개이며 직접 계산으로 대체하지 않습니다.')
        metrics = {}

        def metric(key, field, period, unit='배', positive=True, denominator=None):
            value = number(info.get(field))
            base = number(info.get(denominator)) if denominator else None
            usable = value is not None and (not positive or value > 0) and (base is None or base > 0)
            metrics[key] = {'value': value if usable else None, 'status': 'ok' if usable else 'unavailable',
                            'field': field, 'period': period, 'unit': unit,
                            'reason': '' if usable else '값 미제공 또는 분모/배수가 양수가 아니어서 비교 불가'}

        metric('trailing_pe', 'trailingPE', '최근 12개월 실적(TTM)', denominator='trailingEps')
        metric('forward_pe', 'forwardPE', '공급자 예상치; 예상 회계기간 미확인', denominator='forwardEps')
        metric('price_to_book', 'priceToBook', '공급자 최근 장부가치; 결산일 미확인', denominator='bookValue')
        metric('roe', 'returnOnEquity', '공급자 최근 수익성; 정확한 집계기간 미확인', '%', positive=False)
        if metrics['roe']['value'] is not None:
            metrics['roe']['value'] *= 100
        metric('ev_to_ebitda', 'enterpriseToEbitda', '공급자 EBITDA; 정확한 집계기간 미확인', denominator='ebitda')
        # 이 응답에는 FCF 수익률 완성값이 없으므로 직접 나눠서 만들지 않는다.
        metrics['fcf_yield'] = {
            'value': None, 'status': 'unavailable', 'unit': '%', 'period': '',
            'reason': '공급자가 계산한 FCF 수익률 미제공. 직접 계산하지 않습니다.',
        }
        timestamp = number(info.get('regularMarketTime'))
        try:
            price_as_of = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None
        except (ValueError, OverflowError, OSError):
            price_as_of = None
        missing = [key for key, item in metrics.items() if item['status'] != 'ok']
        return meta('Yahoo Finance', 'partial' if any(m['status'] == 'ok' for m in metrics.values()) else 'unavailable',
                    symbol=symbol, stock=stock, currency=info.get('currency'),
                    financial_currency=info.get('financialCurrency'), as_of=None, price_as_of=price_as_of,
                    sector=info.get('sector'), industry=info.get('industry'), metrics=metrics,
                    url=f'https://finance.yahoo.com/quote/{symbol}/key-statistics/',
                    benchmark={'status': 'unavailable', 'trailing_pe': None, 'forward_pe': None,
                               'price_to_book': None, 'reason': '동종업계 비교 공급원 미연결. 기업 지표를 업종 평균으로 사용하지 마세요.'},
                    historical={'status': 'unavailable', 'reason': '당시 가격·당시 실적 기준의 과거 배수 자료 미연결'},
                    missing=missing + ['industry_benchmark', 'historical_valuation'],
                    limitations=['조회 시각은 재무·예상치 기준일이 아닙니다. price_as_of는 주가 시각만 나타냅니다.',
                                 '선행 PER의 예상 기간을 확인하기 전에는 실적 PER·다른 공급자 배수와 직접 비교하지 마세요.',
                                 'PER·PBR·ROE·EV/EBITDA는 Yahoo 제공 값입니다. DART/SEC 실적으로 직접 계산하거나 혼합하지 않습니다.',
                                 'FCF 수익률은 공급자 완성값 미제공으로 계산하지 않습니다.',
                                 '과거 배수·동종기업 평균·역산 성장 기대·ROIC는 제공하지 않습니다.'])
    return cached(('valuation', 'yahoo-provider-v2', stock['exchange'], stock['code']), 300, fetch)

"""미국 시장 공통 지표. LLM/종목 조회와 독립적이며 실패도 잠시 캐시한다."""
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
import logging
import math
import os
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from dotenv import dotenv_values

VIX_URL = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
OANOR_URL = 'https://api.oanor.com/stockfeargreed-api/v1/index'
FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id='
SOURCES = {
    'vix': ('Cboe', 'https://www.cboe.com/tradable_products/vix/vix_historical_data'),
    'buffett': ('FRED / Fed / BEA', 'https://fred.stlouisfed.org/series/NCBEILQ027S'),
    'fear_greed': ('CNN / OANOR', 'https://www.oanor.com/api/stockfeargreed-api'),
}
_CACHE = {}
_LOCKS = {key: Lock() for key in SOURCES}
_LOGGER = logging.getLogger(__name__)


def log_failure(error, *, stage, started, **context):
    # 원본 예외 메시지/URL/헤더/본문에는 비밀값이 섞일 수 있어 기록하지 않는다.
    # HTTP 코드, DNS/TLS/timeout 예외 타입과 errno만으로 전송 실패를 구분한다.
    reason = getattr(error, 'reason', None)
    fields = dict(event='market_indicator_failure', stage=stage,
                  elapsed_ms=round((monotonic() - started) * 1000),
                  error_type=type(error).__name__, **context)
    if isinstance(error, HTTPError):
        fields['http_status'] = error.code
    if isinstance(reason, BaseException):
        fields['reason_type'] = type(reason).__name__
    for key in ('errno', 'verify_code'):
        value = getattr(reason if isinstance(reason, BaseException) else error, key, None)
        if isinstance(value, int):
            fields[key] = value
    _LOGGER.warning('%s', json.dumps(fields, ensure_ascii=True))


def read_public(url, *, timeout=10):
    # 고정된 공급자 URL만 사용한다. 차단 우회·인증·무제한 재시도는 하지 않는다.
    with urlopen(Request(url, headers={'User-Agent': 'MiniAIResearch/0.1'}), timeout=timeout) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('oversized response')
    return raw.decode('utf-8-sig')


def finite(value):
    if isinstance(value, bool):
        raise ValueError('invalid number')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('invalid number')
    return result


def vix():
    values = {}
    for row in csv.DictReader(StringIO(read_public(VIX_URL))):
        try:
            day = datetime.strptime(row['DATE'], '%m/%d/%Y').date()
            value = finite(row['CLOSE'])
            if value >= 0 and day <= datetime.now(timezone.utc).date():
                values[day] = value
        except (KeyError, ValueError, TypeError):
            continue
    day = max(values)
    return {'value': values[day], 'unit': '', 'as_of': day.isoformat(), 'period': day.isoformat(),
            'description': 'S&P 500 옵션 기반 기대 변동성 · 일별 종가, 실시간 아님'}


def quarterly(series):
    started = monotonic()
    stage = 'fetch_csv'
    rows, rejected = 0, 0
    try:
        raw = read_public(FRED_URL + series, timeout=25)
        stage = 'csv_columns'
        reader = csv.DictReader(StringIO(raw))
        if not {'observation_date', series}.issubset(reader.fieldnames or []):
            raise ValueError('missing required columns')
        stage = 'valid_observations'
        values = {}
        for row in reader:
            rows += 1
            try:
                day = date.fromisoformat(row['observation_date'])
                value = finite(row[series])
                if value > 0 and day <= datetime.now(timezone.utc).date():
                    values[(day.year, (day.month - 1) // 3 + 1)] = value
                else:
                    rejected += 1
            except (KeyError, ValueError, TypeError):
                rejected += 1
        if not values:
            raise ValueError('no valid observations')
        return values
    except Exception as error:
        log_failure(error, stage=stage, started=started, indicator='buffett',
                    provider='FRED', series=series, rows=rows, rejected_rows=rejected)
        raise


def buffett():
    equities, gdp = quarterly('NCBEILQ027S'), quarterly('GDP')
    # 같은 분기의 관측값만 결합한다. 주식가치 백만 달러, GDP 십억 달러(연율).
    common = equities.keys() & gdp.keys()
    if not common:
        error = ValueError('no common quarter')
        log_failure(error, stage='common_quarter', started=monotonic(),
                    indicator='buffett', provider='FRED',
                    equities_periods=len(equities), gdp_periods=len(gdp))
        raise error
    year, quarter = max(common)
    key = (year, quarter)
    return {'value': equities[key] / (gdp[key] * 1000) * 100, 'unit': '%',
            'as_of': f'{year}-{(quarter - 1) * 3 + 1:02d}-01', 'period': f'{year} Q{quarter}',
            'proxy': True, 'method': 'NCBEILQ027S / (GDP * 1000) * 100',
            'inputs': {'equities_million_usd': equities[key], 'gdp_billion_usd_saar': gdp[key]},
            'gdp_url': 'https://fred.stlouisfed.org/series/GDP',
            'description': '분기 대체 지표: 미국 비금융기업 주식가치 / 명목 GDP(연율). 전체 상장시장 시가총액과 범위가 다르며 실시간 버핏지수가 아닙니다. 공표 후 수정될 수 있습니다.'}


class IndicatorError(Exception):
    """화면에 노출해도 되는 고정 오류 문구만 담는다."""


def oanor_key():
    # 키를 응답/로그/URL에 넣지 않는다. 환경변수가 로컬 .env보다 우선한다.
    values = dotenv_values(Path(__file__).resolve().parents[2] / '.env')
    return (os.environ.get('OANOR_API_KEY', values.get('OANOR_API_KEY') or '')).strip()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 인증 헤더를 다른 주소에 전달하지 않는다.
        raise IndicatorError('OANOR 연결 주소 변경 · 조회 불가')


def read_oanor():
    key = oanor_key()
    if not key or key == 'aaa':
        raise IndicatorError('OANOR API 키 미설정')
    request = Request(OANOR_URL, headers={'x-oanor-key': key, 'Accept': 'application/json',
                                        'User-Agent': 'MiniAIResearch/0.1'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(2_000_001)
    except HTTPError as error:
        error.close()
        messages = {401: 'OANOR API 키 인증 실패', 402: 'OANOR 상품 구독 확인 필요',
                    403: 'OANOR 접근 권한 확인 필요', 429: 'OANOR 호출 한도 초과',
                    502: 'OANOR 원본 데이터 조회 실패'}
        raise IndicatorError(messages.get(error.code, 'OANOR 서비스 응답 오류')) from None
    if len(raw) > 2_000_000:
        raise ValueError('oversized response')
    return json.loads(raw.decode('utf-8-sig'))


def fear_greed():
    payload = read_oanor()
    if payload.get('success') is not True or payload.get('status') != 'ok':
        raise IndicatorError('OANOR 데이터 조회 실패')
    data = payload['data']
    value = finite(data['score'])
    if not 0 <= value <= 100:
        raise ValueError('score range')
    stamp = datetime.fromisoformat(data['updated'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError('invalid timestamp')
    ratings = {'extreme fear': '극도의 공포', 'fear': '공포', 'neutral': '중립',
               'greed': '탐욕', 'extreme greed': '극도의 탐욕'}
    return {'value': value, 'unit': '/100', 'as_of': stamp.isoformat(),
            'period': stamp.date().isoformat(), 'rating': ratings.get(str(data.get('rating', '')).lower(), ''),
            'description': 'CNN 미국 주식 공포·탐욕지수 · OANOR 중계. 기준일은 공급자가 반환한 데이터 갱신 시각이며 실시간 보장이 아닙니다.'}


def indicator(name):
    if name not in SOURCES:
        raise ValueError('unknown indicator')
    with _LOCKS[name]:
        previous = _CACHE.get(name)
        if previous and monotonic() - previous[0] < 300:
            return deepcopy(previous[1])
        source, url = SOURCES[name]
        checked = datetime.now(timezone.utc).isoformat()
        started = monotonic()
        try:
            result = {**{'vix': vix, 'buffett': buffett, 'fear_greed': fear_greed}[name](),
                      'status': 'ok', 'fetched_at': checked}
        except Exception as error:
            log_failure(error, stage='indicator_result', started=started, indicator=name,
                        checked_at=checked, previous_value_available=bool(previous and previous[1].get('value') is not None),
                        failure_cache_seconds=300)
            message = str(error) if isinstance(error, IndicatorError) else '공급원 접근 제한 또는 자료 조회 실패'
            # 실패를 0으로 바꾸지 않는다. 마지막 값은 기준일과 갱신 실패 상태를 유지한다.
            if previous and previous[1].get('value') is not None:
                result = {**previous[1], 'status': 'stale', 'message': message + ' · 이전 자료'}
            else:
                result = {'status': 'unavailable', 'value': None, 'as_of': None,
                          'message': message}
        result.update(id=name, source=source, url=url, market='US', checked_at=checked)
        _CACHE[name] = (monotonic(), result)
        return deepcopy(result)

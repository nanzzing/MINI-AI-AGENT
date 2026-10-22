"""미국 시장 공통 지표. LLM/종목 조회와 독립적이며 실패도 잠시 캐시한다."""
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
import math
from threading import Lock
from time import monotonic
from urllib.request import Request, urlopen

VIX_URL = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
CNN_URL = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id='
SOURCES = {
    'vix': ('Cboe', 'https://www.cboe.com/tradable_products/vix/vix_historical_data'),
    'buffett': ('FRED / Fed / BEA', 'https://fred.stlouisfed.org/series/NCBEILQ027S'),
    'fear_greed': ('CNN', 'https://www.cnn.com/markets/fear-and-greed'),
}
_CACHE = {}
_LOCKS = {key: Lock() for key in SOURCES}


def read_public(url):
    # 고정된 공급자 URL만 사용한다. 차단 우회·인증·무제한 재시도는 하지 않는다.
    with urlopen(Request(url, headers={'User-Agent': 'MiniAIResearch/0.1'}), timeout=10) as response:
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
    values = {}
    for row in csv.DictReader(StringIO(read_public(FRED_URL + series))):
        try:
            day = date.fromisoformat(row['observation_date'])
            value = finite(row[series])
            if value > 0 and day <= datetime.now(timezone.utc).date():
                values[(day.year, (day.month - 1) // 3 + 1)] = value
        except (KeyError, ValueError, TypeError):
            continue
    return values


def buffett():
    equities, gdp = quarterly('NCBEILQ027S'), quarterly('GDP')
    # 같은 분기의 관측값만 결합한다. 주식가치 백만 달러, GDP 십억 달러(연율).
    year, quarter = max(equities.keys() & gdp.keys())
    key = (year, quarter)
    return {'value': equities[key] / (gdp[key] * 1000) * 100, 'unit': '%',
            'as_of': f'{year}-{(quarter - 1) * 3 + 1:02d}-01', 'period': f'{year} Q{quarter}',
            'proxy': True, 'method': 'NCBEILQ027S / (GDP * 1000) * 100',
            'inputs': {'equities_million_usd': equities[key], 'gdp_billion_usd_saar': gdp[key]},
            'gdp_url': 'https://fred.stlouisfed.org/series/GDP',
            'description': '분기 대체 지표: 미국 비금융기업 주식가치 / 명목 GDP(연율). 전체 상장시장 시가총액과 범위가 다르며 실시간 버핏지수가 아닙니다. 공표 후 수정될 수 있습니다.'}


def fear_greed():
    data = json.loads(read_public(CNN_URL))['fear_and_greed']
    value = finite(data['score'])
    if not 0 <= value <= 100:
        raise ValueError('score range')
    stamp = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError('invalid timestamp')
    ratings = {'extreme fear': '극도의 공포', 'fear': '공포', 'neutral': '중립',
               'greed': '탐욕', 'extreme greed': '극도의 탐욕'}
    return {'value': value, 'unit': '/100', 'as_of': stamp.isoformat(),
            'period': stamp.date().isoformat(), 'rating': ratings.get(str(data.get('rating', '')).lower(), ''),
            'description': '미국 주식 공포·탐욕지수. CNN 웹 데이터 경로는 비공식 연동으로 변경·차단될 수 있습니다. 암호화폐 지수가 아닙니다.'}


def indicator(name):
    if name not in SOURCES:
        raise ValueError('unknown indicator')
    with _LOCKS[name]:
        previous = _CACHE.get(name)
        if previous and monotonic() - previous[0] < 300:
            return deepcopy(previous[1])
        source, url = SOURCES[name]
        checked = datetime.now(timezone.utc).isoformat()
        try:
            result = {**{'vix': vix, 'buffett': buffett, 'fear_greed': fear_greed}[name](),
                      'status': 'ok', 'fetched_at': checked}
        except Exception:
            # 실패를 0으로 바꾸지 않는다. 마지막 값은 기준일과 갱신 실패 상태를 유지한다.
            if previous and previous[1].get('value') is not None:
                result = {**previous[1], 'status': 'stale', 'message': '갱신 실패 · 이전 자료'}
            else:
                result = {'status': 'unavailable', 'value': None, 'as_of': None,
                          'message': '공급원 접근 제한 또는 자료 조회 실패'}
        result.update(id=name, source=source, url=url, market='US', checked_at=checked)
        _CACHE[name] = (monotonic(), result)
        return deepcopy(result)

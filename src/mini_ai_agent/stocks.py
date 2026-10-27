"""한국투자증권 종목 마스터를 내려받고 로컬 종목 검색에 사용한다.

refresh는 수동 갱신 명령에서 실행한다. 명시적 검색은 로컬 실패 시 외부 후보를 조회한다.
여기서 다루는 자료는 종목 식별 정보이며 시세·재무 데이터는 포함하지 않는다.
"""

from datetime import datetime, timezone
from functools import lru_cache
import csv
import io
import json
from pathlib import Path
import re
from copy import deepcopy
from threading import RLock
from time import monotonic
import yfinance as yf
from urllib.request import urlopen
from zipfile import ZipFile

CATALOG = Path(__file__).parent / "data" / "stocks.json"
# 종목 코드는 계산할 숫자가 아니라 식별자다. '005930'의 앞자리 0을 보존하기 위해
# 문자열로 다룬다. 미국 티커도 거래소와 함께 사용해야 종목 식별이 명확해진다.
BASE_URL = "https://new.real.download.dws.co.kr/common/master/"
# (원본 파일, 시장명, 국내 고정 길이 부가정보 크기). 미국 파일은 탭 구분이라 None이다.
SOURCES = (
    ("kospi_code.mst", "KOSPI", 227),
    ("kosdaq_code.mst", "KOSDAQ", 221),
    ("konex_code.mst", "KONEX", 184),
    ("nasmst.cod", "NASDAQ", None),
    ("nysmst.cod", "NYSE", None),
    ("amsmst.cod", "NYSE American", None),
)
ALIASES = {"삼전": "005930", "애플": "AAPL", "테슬라": "TSLA", "엔비디아": "NVDA"}
# 별칭은 편의 검색용 일부 예시다. 이 사전에 없는 종목도 아래 전체 카탈로그에서
# 정식 이름이나 티커로 검색된다. 따라서 삼성전자 전용 서비스인 것은 아니다.
# 기술 지표와 겹치는 영문 약어는 채팅의 종목 전환을 강제하는 근거로 쓰지 않는다.
# 실제 해당 종목을 검색하는 도구/검색창에서는 그대로 검색할 수 있다.
INDICATOR_TICKERS = {"rsi", "macd", "ma", "sma", "ema", "atr", "adx", "obv", "cci", "roc"}
_DISCOVERED = {}
_SEARCH_CACHE = {}
_SEARCH_LOCK = RLock()


def ticker_key(code):
    return re.sub(r'[.\-]', '/', code.casefold())


def known_stocks():
    """번들 목록과 외부에서 확인한 종목을 합친다. 외부 종목은 프로세스 내 24시간 보관."""
    rows = list(load_catalog()['stocks'])
    with _SEARCH_LOCK:
        expired = [key for key, (stamp, _) in _DISCOVERED.items() if monotonic() - stamp >= 86400]
        for key in expired:
            del _DISCOVERED[key]
        keys = {(row['exchange'], ticker_key(row['code'])) for row in rows}
        rows.extend(deepcopy(row) for _, row in _DISCOVERED.values()
                    if (row['exchange'], ticker_key(row['code'])) not in keys)
    return rows


def search_stocks(query):
    """명시적 검색만 외부 조회한다. 일반 대화의 자동 종목 판정은 로컬에 머문다."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise ValueError('검색어는 1~200자여야 합니다.')
    query = query.strip()
    matches = find_stocks(query)
    if not matches:
        compact = re.sub(r'\s+', '', query.casefold())
        if len(compact) >= 2:
            matches = [row for row in known_stocks() if compact in re.sub(r'\s+', '', row['name'].casefold())]
    if matches:
        return {'stocks': matches[:20], 'status': 'ok', 'source': 'KIS directory + verified search cache',
                'fetched_at': load_catalog()['fetched_at']}
    key = query.casefold()
    with _SEARCH_LOCK:
        item = _SEARCH_CACHE.get(key)
        if item and monotonic() - item[0] < item[1]:
            return deepcopy(item[2])
    result = {'stocks': [], 'source': 'Yahoo Finance Search',
              'fetched_at': datetime.now(timezone.utc).isoformat()}
    try:
        quotes = yf.Search(query, max_results=20, news_count=0, lists_count=0,
                           include_cb=False, recommended=0, timeout=10).quotes
        rows = {}
        for quote in quotes:
            if not isinstance(quote, dict) or quote.get('quoteType') != 'EQUITY':
                continue
            symbol, exchange = quote.get('symbol', ''), quote.get('exchange', '')
            if not isinstance(symbol, str):
                continue
            if exchange in {'KSC', 'KOE'}:
                market, suffix = ('KOSPI', '.KS') if exchange == 'KSC' else ('KOSDAQ', '.KQ')
                if not re.fullmatch(r'\d{6}' + re.escape(suffix), symbol):
                    continue
                code = symbol[:-3]
            else:
                market = {'NYQ': 'NYSE', 'NMS': 'NASDAQ', 'NGM': 'NASDAQ', 'NCM': 'NASDAQ', 'ASE': 'NYSE American'}.get(exchange)
                if not market or not re.fullmatch(r'[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,4})?', symbol):
                    continue
                code = ticker_key(symbol).upper()
            name = quote.get('longname') or quote.get('shortname')
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                continue
            rows[(market, code)] = {'name': name.strip(), 'code': code, 'exchange': market}
        result.update(stocks=list(rows.values()), status='ok' if rows else 'empty')
        with _SEARCH_LOCK:
            for identity, row in rows.items():
                if len(_DISCOVERED) >= 512:
                    _DISCOVERED.pop(next(iter(_DISCOVERED)))
                _DISCOVERED[identity] = (monotonic(), row)
        if not rows:
            result['message'] = '후보가 없습니다. 영문 회사명이나 정확한 티커로 재검색하세요. A/B·우선주는 구분해야 합니다.'
    except Exception:
        result.update(status='unavailable', message='외부 종목 검색에 실패했습니다. 종목이 없다는 뜻은 아닙니다. 영문명·티커로 재시도하거나 잠시 후 확인하세요.')
    with _SEARCH_LOCK:
        if len(_SEARCH_CACHE) >= 256:
            _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))
        _SEARCH_CACHE[key] = (monotonic(), 30 if result['status'] == 'unavailable' else 600, deepcopy(result))
    return result


def parse_master(content: str, exchange: str, tail_length: int | None) -> list[dict]:
    """서로 다른 국내·미국 원본 형식을 name/code/exchange 구조로 통일한다."""
    rows = []
    if tail_length is not None:
        for line in content.splitlines():
            if len(line) < 21 + tail_length:
                raise ValueError(f"Invalid master row: {exchange}")
            prefix, fields = line[:-tail_length], line[-tail_length:]
            # 국내 파일 앞부분은 코드·표준코드·이름, 뒷부분은 고정 길이 부가정보다.
            # 증권 그룹 ST만 남겨 ETF·ETN 등을 제외하고, 코드는 문자열로 보관한다.
            if fields[:2] != "ST":
                continue
            rows.append({"name": prefix[21:].strip(), "code": prefix[:9].strip(), "exchange": exchange})
    else:
        # 미국 파일은 첫 행부터 데이터다. 9번째 필드의 2는 주식, 3은 ETF 등 ETP를 뜻한다.
        for fields in csv.reader(io.StringIO(content), delimiter="\t"):
            if len(fields) < 9:
                raise ValueError(f"Invalid master row: {exchange}")
            if fields[8].strip() != "2":
                continue
            rows.append({"name": fields[6].strip() or fields[7].strip(),
                         "code": fields[4].strip(), "exchange": exchange})
    if not rows or any(not row["name"] or not row["code"] for row in rows):
        raise ValueError(f"Empty or invalid directory: {exchange}")
    return rows


def refresh() -> dict:
    """6개 시장을 모두 다운로드·검증한 후 기존 종목 목록을 한 번에 교체한다."""
    rows, sources = [], []
    # 카탈로그 갱신은 명시적으로 실행한 때만 발생한다. 서버 시작/채팅마다 다운로드하지 않는다.
    for filename, exchange, tail_length in SOURCES:
        url = f"{BASE_URL}{filename}.zip"
        with urlopen(url, timeout=40) as response:
            archive = response.read()
        with ZipFile(io.BytesIO(archive)) as zipped:
            # 압축을 디스크에 풀지 않고 필요한 파일만 읽는다. 원본 파일명의 대소문자는 다를 수 있다.
            member = next(name for name in zipped.namelist() if name.casefold() == filename.casefold())
            content = zipped.read(member).decode("cp949")
        parsed = parse_master(content, exchange, tail_length)
        rows.extend(parsed)
        sources.append({"url": url, "exchange": exchange, "count": len(parsed)})
        print(f"{exchange}: {len(parsed)}", flush=True)
    # 거래소가 다르면 같은 코드가 있을 수 있어 두 값을 묶어 중복을 검사한다.
    keys = {(row["exchange"], row["code"]) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("Duplicate stock identifiers")
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(),
               "provider": "Korea Investment & Securities", "sources": sources,
               "stocks": sorted(rows, key=lambda row: (row["exchange"], row["code"]))}
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    # 중간 실패로 기존 파일이 손상되지 않도록 임시 파일 작성이 끝난 뒤 교체한다.
    temporary = CATALOG.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CATALOG)
    load_catalog.cache_clear()  # 현재 프로세스도 다음 검색부터 갱신한 파일을 읽는다.
    return payload


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    """한 프로세스에서 처음 읽은 목록을 캐시해 메시지마다 파일을 읽지 않는다."""
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def find_stocks(message: str, *, allow_indicator_tickers: bool = True) -> list[dict]:
    """별칭·코드·종목명을 찾는다. 채팅 자동 판정은 지표와 겹치는 티커를 제외한다.

    명시적인 종목 검색은 기본값 True를 사용한다. 채팅에서는 False로 호출하되
    '$RSI', '티커 RSI', 'NYSE:RSI'처럼 종목임을 명시한 표기는 계속 허용한다.
    """
    normalized = message.casefold()
    rows = known_stocks()
    compact = re.sub(r'\s+', '', normalized)
    matches = []
    for row in rows:
        name = re.sub(r'\s+', '', row['name'].casefold())
        index = compact.find(name)
        if index < 0:
            continue
        # 짧은 영문 회사명(NC 등)을 영단어 내부에서 인식하지 않는다.
        if name[0].isascii() and name[0].isalnum() and index and compact[index - 1].isascii() and compact[index - 1].isalnum():
            continue
        end = index + len(name)
        if name[-1].isascii() and name[-1].isalnum() and end < len(compact) and compact[end].isascii() and compact[end].isalnum():
            continue
        matches.append(row)
    if matches:
        longest = max(len(re.sub(r'\s+', '', row['name'])) for row in matches)
        matches = [row for row in matches if len(re.sub(r'\s+', '', row['name'])) == longest]
    # 회사명의 Class B를 별도 티커 B로 잘못 읽지 않도록 이름 부분을 가린다.
    remaining = normalized
    for row in matches:
        pattern = r'\s*'.join(re.escape(char) for char in re.sub(r'\s+', '', row['name'].casefold()))
        remaining = re.sub(pattern, ' ', remaining)
    # 대소문자·공백·티커 구분자를 정규화한다. 이 자동 판정 함수는 외부 검색을 하지 않는다.
    # 반환 타입을 항상 리스트로 유지해 0개/1개/여러 후보를 호출자가 구분할 수 있게 한다.
    # 티커 중간의 점·하이픈 등은 유지한다. 별칭도 코드 후보로 바꾸어 같은 검색에 사용한다.
    codes = set(re.findall(r"(?<![a-z0-9])[a-z0-9]+(?:[.\-/][a-z0-9]+)*(?![a-z0-9])", remaining))
    if not allow_indicator_tickers:
        exchanges = r"(?:nyse(?:\s+american)?|nasdaq|amex|kospi|kosdaq|konex)"
        for code in codes & INDICATOR_TICKERS:
            ticker = re.escape(code)
            explicit = (
                rf"(?<![a-z0-9])(?:\$\s*|(?:티커|종목\s*코드|ticker|symbol)\s*[:=]?\s*|{exchanges}\s*:\s*){ticker}(?![a-z0-9])"
                rf"|(?<![a-z0-9]){ticker}\s*\(\s*{exchanges}\s*\)"
            )
            if not re.search(explicit, normalized):
                codes.remove(code)
    for alias, code in ALIASES.items():
        if alias in normalized:
            codes.add(code.casefold())
    codes = {ticker_key(code) for code in codes}
    exact = [row for row in rows if ticker_key(row["code"]) in codes]
    identities = {(row['exchange'], row['code']): row for row in matches + exact}
    return list(identities.values())


if __name__ == "__main__":
    # python -m mini_ai_agent.stocks로 실행할 때만 네트워크 다운로드를 수행한다.
    result = refresh()
    print(f"Saved {len(result['stocks'])} stocks to {CATALOG}")

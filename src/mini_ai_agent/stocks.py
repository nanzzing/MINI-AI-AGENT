"""한국투자증권 종목 마스터를 내려받고 로컬 종목 검색에 사용한다.

refresh는 수동 갱신 명령에서 실행하고, 채팅 중에는 저장된 JSON만 검색한다.
여기서 다루는 자료는 종목 식별 정보이며 시세·재무 데이터는 포함하지 않는다.
"""

from datetime import datetime, timezone
from functools import lru_cache
import csv
import io
import json
from pathlib import Path
import re
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
    # 대소문자 차이만 정규화한다. 오타 교정·의미 검색·외부 검색은 하지 않는 규칙 검색이다.
    # 반환 타입을 항상 리스트로 유지해 0개/1개/여러 후보를 호출자가 구분할 수 있게 한다.
    # 티커 중간의 점·하이픈 등은 유지한다. 별칭도 코드 후보로 바꾸어 같은 검색에 사용한다.
    codes = set(re.findall(r"(?<![a-z0-9])[a-z0-9]+(?:[.\-/][a-z0-9]+)*(?![a-z0-9])", normalized))
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
    rows = load_catalog()["stocks"]
    exact = [row for row in rows if row["code"].casefold() in codes]
    if exact:
        return exact
    matches = [row for row in rows if row["name"].casefold() in normalized]
    if matches:
        # '삼성전자우'에 '삼성전자'도 포함되므로 가장 긴 이름을 우선해 우선주를 구분한다.
        longest = max(len(row["name"]) for row in matches)
        return [row for row in matches if len(row["name"]) == longest]
    return []


if __name__ == "__main__":
    # python -m mini_ai_agent.stocks로 실행할 때만 네트워크 다운로드를 수행한다.
    result = refresh()
    print(f"Saved {len(result['stocks'])} stocks to {CATALOG}")

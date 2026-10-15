"""첫 화면 후보: 국가별 뉴스 언급 1개와 거래량 상위 1개. 매수 추천이 아니다."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yfinance as yf

from mini_ai_agent.market_data import cached, meta, yahoo_symbol
from mini_ai_agent.stocks import load_catalog


def news_candidate(region, stocks):
    """수집한 RSS 표본 안에서 서로 다른 제목의 언급 수를 센다. 전체 뉴스 순위는 아니다."""
    korean = region == "kr"
    params = {"q": "주식 기업 when:1d" if korean else "stocks company when:1d",
              "hl": "ko" if korean else "en-US", "gl": "KR" if korean else "US",
              "ceid": "KR:ko" if korean else "US:en"}
    url = "https://news.google.com/rss/search?" + urlencode(params)
    with urlopen(Request(url, headers={"User-Agent": "MiniAIResearch/0.1"}), timeout=12) as response:
        root = ET.fromstring(response.read(2_000_000))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    articles, seen = [], set()
    for item in root.findall("./channel/item"):
        title = item.findtext("title", "")
        normalized = re.sub(r"\W+", "", title.rsplit(" - ", 1)[0]).casefold()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
        except (ValueError, TypeError):
            continue
        if not normalized or normalized in seen or not cutoff <= published <= datetime.now(timezone.utc):
            continue
        seen.add(normalized)
        articles.append({"title": title, "url": item.findtext("link", ""), "published_at": published.isoformat()})
    ranked = []
    for stock in stocks:
        # 미국 단문 티커는 일반 단어와 혼동되므로 $티커/괄호 티커만 인정한다.
        name = stock["name"]
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
        hits = [article for article in articles if
                (len(name) >= 3 and (name in article["title"] if korean else pattern.search(article["title"])))
                or (not korean and ("$" + stock["code"] in article["title"] or "(" + stock["code"] + ")" in article["title"]))]
        if hits:
            ranked.append({"stock": stock, "region": region, "count": len(hits), "article": hits[0],
                           "source": "Google News RSS", "as_of": hits[0]["published_at"]})
    return sorted(ranked, key=lambda row: (-row["count"], row["stock"]["code"]))[:1]


def active_candidate(region, stocks):
    """시장별 거래량 순위. 거래대금/자금 순유입 순위와 혼동하지 않는다."""
    result = yf.screen(yf.EquityQuery("eq", ["region", region]),
                       sortField="dayvolume", sortAsc=False, size=25)
    catalog = {yahoo_symbol(stock): stock for stock in stocks if stock["exchange"] != "KONEX"}
    for quote in result.get("quotes", []):
        stock = catalog.get(quote.get("symbol"))
        volume, stamp = quote.get("regularMarketVolume"), quote.get("regularMarketTime")
        if stock and isinstance(volume, (int, float)) and volume > 0 and stamp:
            return [{"stock": stock, "region": region, "volume": volume,
                     "change_pct": quote.get("regularMarketChangePercent"), "source": "Yahoo Finance",
                     "as_of": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}]
    return []


def discover():
    """방문마다 모델을 호출하지 않는다. 5분 캐시, 공급자 실패는 빈 상태로 정직하게 표시한다."""
    def fetch():
        rows = load_catalog()["stocks"]
        groups = {"kr": [r for r in rows if r["exchange"] in {"KOSPI", "KOSDAQ", "KONEX"}],
                  "us": [r for r in rows if r["exchange"] in {"NASDAQ", "NYSE", "NYSE American"}]}
        output = meta("Google News RSS / Yahoo Finance", news=[], active=[], missing=[])
        for category, loader in [("news", news_candidate), ("active", active_candidate)]:
            for region, stocks in groups.items():
                try:
                    candidates = loader(region, stocks)
                    output[category].extend(candidates)
                    if not candidates:
                        output["missing"].append(f"{category}/{region}")
                except Exception:
                    output["missing"].append(f"{category}/{region}")
        output["status"] = "partial" if output["missing"] else "ok"
        return output
    return cached(("discovery",), 300, fetch)

"""공개 시장 헤드라인. 모델 호출 없이 RSS를 모아 프로세스별 15분 캐시한다."""
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import re
import xml.etree.ElementTree as ET

GROUPS = {
    '국내 증시': '코스피 OR 코스닥 OR "국내 증시"',
    '미국 증시': '"미국 증시" OR "뉴욕 증시" OR 나스닥',
    '금리·환율': '기준금리 OR 연준 OR "원달러 환율"',
    '기업·산업': '"기업 실적" OR "반도체 수출" OR "AI 투자"',
}
# 제목의 언론사 이름만 믿지 않고 RSS source의 원문 도메인도 확인한다.
DOMAINS = {'yna.co.kr', 'yonhapnewstv.co.kr', 'hankyung.com', 'mk.co.kr',
           'sedaily.com', 'edaily.co.kr', 'mt.co.kr', 'newsis.com', 'news1.kr',
           'hankookilbo.com', 'hani.co.kr', 'khan.co.kr', 'chosun.com',
           'chosunbiz.com', 'biz.chosun.com', 'joongang.co.kr', 'donga.com',
           'kbs.co.kr', 'sbs.co.kr', 'imbc.com', 'ytn.co.kr', 'asiae.co.kr',
           'heraldcorp.com', 'fnnews.com', 'etnews.com'}
_lock = Lock()
_cache = None
_checked = 0


def parse_feed(body, group, now):
    rows = []
    for item in ET.fromstring(body).findall('./channel/item'):
        source = item.find('source')
        host = urlparse(source.get('url', '') if source is not None else '').hostname or ''
        if not any(host == d or host.endswith('.' + d) for d in DOMAINS):
            continue
        title = item.findtext('title', '').rsplit(' - ', 1)[0].strip()
        # RSS에도 종목 시세/정보 페이지가 섞인다. 종목명+코드만 있는 결과는 기사가 아니다.
        if re.fullmatch(r'[\w .&·-]+\s*\(\s*(?:\d{6}|[A-Z]{1,5}(?:[.-][A-Z])?)\s*\)', title):
            continue
        if re.search(r'(?:실시간\s*시세|종목\s*(?:정보|시세))\s*$', title):
            continue
        link = item.findtext('link', '')
        parsed = urlparse(link)
        if not re.search('[가-힣]', title) or parsed.scheme != 'https' or parsed.hostname != 'news.google.com':
            continue
        try:
            stamp = parsedate_to_datetime(item.findtext('pubDate', ''))
            if stamp.tzinfo is None or not now - timedelta(hours=72) <= stamp <= now:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        rows.append(dict(title=title, url=link, publisher=source.text or host,
                         published_at=stamp.isoformat(), group=group))
    return sorted(rows, key=lambda r: r['published_at'], reverse=True)


def event_key(title):
    """제목만으로 명확한 사건을 묶는다. 전망과 실제 결정, 국가/방향은 구분한다."""
    text = re.sub(r'\s+', '', title).casefold()
    if re.search(r'전망|예상|가능성|할까|관측', text):
        return None
    if re.search(r'원[/·ㆍ-]?달러|달러[/·ㆍ-]?원', text) and '환율' in text:
        amounts = [float(n.replace(',', '')) for n in re.findall(r'(\d[\d,]*(?:\.\d+)?)원', text)]
        levels = [n for n in amounts if n >= 100]
        direction = 'down' if re.search(r'내린|하락', text) else 'up' if re.search(r'오른|상승', text) else None
        if len(levels) == 1 and direction:
            return ('fx', 'usdkrw', levels[0], direction)
    banks = {'호주': r'호주|rba', '미국': r'미국|연준|fed|fomc',
             '한국': r'한국은행|한은|금통위', '일본': r'일본|boj',
             '유럽': r'유럽중앙은행|ecb', '영국': r'영국|boe',
             '캐나다': r'캐나다', '뉴질랜드': r'뉴질랜드'}
    if '금리' in text:
        countries = [country for country, pattern in banks.items() if re.search(pattern, text)]
        actions = [a for a in ('인상', '인하', '동결') if a in text]
        rates = re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)%(?!p|포인트)', text)
        if len(countries) == len(actions) == len(rates) == 1:
            return ('rate', countries[0], actions[0], float(rates[0]))
    markets = [m for m in ('뉴욕증시', '코스피', '코스닥') if m in text]
    phases = [p for p in ('출발', '마감') if p in text]
    directions = re.findall(r'(혼조|상승|하락)(?=출발|마감)', text)
    if len(markets) == len(phases) == len(directions) == 1:
        return ('market', markets[0], phases[0], directions[0])
    return None


def same_story(left, right):
    # 날짜가 다른 별개 발표/장세까지 합치지 않는다.
    if left.get('published_at') and right.get('published_at'):
        delta = datetime.fromisoformat(left['published_at']) - datetime.fromisoformat(right['published_at'])
        if abs(delta.total_seconds()) > 24 * 3600:
            return False
    a, b = left['title'], right['title']
    ka, kb = event_key(a), event_key(b)
    if ka is not None and kb is not None:
        return ka == kb
    # 전망 기사와 확정 사건은 문구가 유사해도 분리한다.
    if bool(re.search(r'전망|예상|가능성|할까|관측', a)) != bool(re.search(r'전망|예상|가능성|할까|관측', b)):
        return False
    normalized = lambda t: re.sub(r'\W+', '', t).casefold()
    return SequenceMatcher(None, normalized(a), normalized(b)).ratio() >= .72


def select_rows(feeds):
    selected, links = [], set()
    # 그룹별 최대 3개. 최근순이라 24시간 내 기사가 우선되고 부족할 때만 72시간까지 사용한다.
    for group, rows in feeds.items():
        count = 0
        for row in rows:
            if row['url'] in links or any(same_story(row, previous) for previous in selected):
                continue
            selected.append(row); links.add(row['url'])
            count += 1
            if count == 3:
                break
    return selected


def headlines():
    global _cache, _checked
    with _lock:
        if _cache is not None and monotonic() - _checked < 900:
            return _cache
        now = datetime.now(timezone.utc)
        feeds, failed = {}, []
        for group, query in GROUPS.items():
            try:
                params = urlencode(dict(q=f'({query}) when:3d', hl='ko', gl='KR', ceid='KR:ko'))
                with urlopen(Request('https://news.google.com/rss/search?' + params,
                                     headers={'User-Agent': 'MiniAIResearch/0.1'}), timeout=8) as response:
                    feeds[group] = parse_feed(response.read(2_000_000), group, now)
            except Exception:
                failed.append(group)
        items = select_rows(feeds)
        # 실패한 그룹만 이전 결과를 유지한다. 오래된 뉴스는 최대 72시간까지만 보존한다.
        if failed and _cache:
            for group in failed:
                feeds[group] = [r for r in _cache['items'] if r['group'] == group
                                and datetime.fromisoformat(r['published_at']) >= now - timedelta(hours=72)]
            items = select_rows(feeds)
        _cache = dict(items=items, status='partial' if failed else 'ok', stale=bool(failed),
                      failed_groups=failed, checked_at=now.isoformat(),
                      fetched_at=(_cache or {}).get('fetched_at') if len(failed) == len(GROUPS) else now.isoformat(),
                      source='Google News RSS')
        _checked = monotonic()
        return _cache

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mini_ai_agent import headlines as h


class HeadlineTests(unittest.TestCase):
    def test_source_language_date_and_link_validation(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        def item(title='한국 증시 상승', source='https://www.hankyung.com', link='https://news.google.com/rss/articles/1', date='Tue, 03 Nov 2026 12:00:00 GMT'):
            return f'<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><source url="{source}">한국경제</source></item>'
        body = '<rss><channel>' + ''.join([
            item(), item(source='https://hankyung.com.evil.test'), item(title='US stocks'),
            item(title='SK스퀘어(402340)'), item(title='삼성전자 종목정보'),
            item(link='javascript:alert(1)'), item(date='bad'),
            item(date='Thu, 01 Jan 2026 00:00:00 GMT')]) + '</channel></rss>'
        rows = h.parse_feed(body, '국내 증시', now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['publisher'], '한국경제')

    def test_stock_code_in_real_headline_is_allowed(self):
        body = '<rss><channel><item><title>SK스퀘어(402340), 반도체 투자 확대</title><link>https://news.google.com/rss/articles/1</link><pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate><source url="https://mk.co.kr">매일경제</source></item></channel></rss>'
        self.assertEqual(len(h.parse_feed(body, '국내 증시', datetime(2026, 11, 4, tzinfo=timezone.utc))), 1)

    def test_duplicate_and_group_limit(self):
        rows = [dict(title=t, url=str(i)) for i, t in enumerate([
            '코스피 급등 외국인 매수', '코스피 급등, 외국인 매수!',
            '미국 고용 전망 변화', '반도체 수출 호조', '은행 대출 규제'])]
        result = h.select_rows({'a': rows, 'b': rows})
        self.assertEqual(len(result), 4)
        self.assertNotIn(rows[1], result)

    def test_failure_preserves_cache_and_throttles_retry(self):
        now = datetime.now(timezone.utc).isoformat()
        old = dict(items=[dict(title='국내 증시', url='https://news.google.com/1',
                              group='국내 증시', published_at=now)], fetched_at=now)
        with patch.object(h, '_cache', old), patch.object(h, '_checked', -10000), \
                patch.object(h, 'urlopen', side_effect=TimeoutError) as fetch:
            result = h.headlines()
            self.assertTrue(result['stale'])
            self.assertEqual(result['items'], old['items'])
            self.assertEqual(result['fetched_at'], now)
            h.headlines()
            self.assertEqual(fetch.call_count, 4)

    def test_rate_event_keeps_one_and_fills_other_topics(self):
        titles = ['호주 중앙은행, 기준금리 4.85%로 인상…18년 만에 최고',
                  "호주중앙은행 기준금리 4.85%…올해 5번째 인상, 6대3 분열",
                  'RBA, 금리 4.85%로 인상 결정', '한국은행 기준금리 3% 동결',
                  '원달러 환율 급락 수출기업 긴장']
        rows = [dict(title=t, url=str(i)) for i, t in enumerate(titles)]
        self.assertEqual(h.select_rows({'금리': rows}), [rows[0], rows[3], rows[4]])

    def test_different_decisions_and_forecasts_remain(self):
        row = lambda t: dict(title=t)
        base = row('호주 기준금리 4.85% 인상')
        for title in ['호주 기준금리 4.85% 동결', '호주 기준금리 4.85% 인상 전망',
                      '영국 기준금리 4.85% 인상', '호주 기준금리 5.1% 인상']:
            self.assertFalse(h.same_story(base, row(title)))
        self.assertFalse(h.same_story(dict(base, published_at='2026-11-01T00:00:00+00:00'),
                                     dict(base, published_at='2026-11-03T00:00:00+00:00')))

    def test_market_opening_reports_merge_but_not_close(self):
        row = lambda t: dict(title=t)
        base = row('뉴욕증시, 美중간선거 당일 혼조 출발…나스닥 0.59%↑')
        self.assertTrue(h.same_story(base, row('뉴욕증시, 혼조출발…유가 하락·美 중간 선거에 촉각')))
        self.assertFalse(h.same_story(base, row('뉴욕증시 혼조 마감')))

    def test_exchange_rate_number_format(self):
        row = lambda t: dict(title=t)
        self.assertTrue(h.same_story(row('원/달러 환율, 2.8원 내린 1,341원…관망세'),
                                     row('원·달러 환율, 2.8원 내린 1341.0원 마감')))
        self.assertFalse(h.same_story(row('원/달러 환율 1341원 상승'), row('원/달러 환율 1341원 하락')))


if __name__ == '__main__':
    unittest.main()

"""근거 선언 검증 테스트. 실제 모델을 호출하지 않고 문장·ID 관계를 직접 구성한다.

    declared는 '모델이 선언했고 ID가 유효함'만 의미한다. 정답/투자 품질 테스트가 아니다.
    원본 수치를 반복하지 않는 종합 결론도 여러 자료에 연결할 수 있는지를 확인한다.
"""
import unittest
from mini_ai_agent.coverage import evidence_bank, declared_coverage, answer_schema, PATHS, FINANCIAL_FIELDS
from mini_ai_agent.conversation import Horizon, RESEARCH_FOCUS


class CoverageTests(unittest.TestCase):
    def setUp(self):
        # 매 테스트마다 독립된 RSI/MACD 자료와 뉴스 조회 실패 상태를 만든다.
        self.bank = evidence_bank(['RSI','MACD','최근 관련 기사 및 주요 뉴스'], {
            'technicals': {'status':'ok','source':'Yahoo','rsi14':34.4,'macd_12_26_9':{'line':-2}},
            'news': {'status':'unavailable','items':[]}})

    def test_combined_conclusion_without_repeating_values(self):
        # 숫자 문자열 매칭 대신 모델이 반환한 두 ID로 결론과 자료를 연결한다.
        answer = '모멘텀 약화에 유의할 필요가 있습니다.'
        claims = [{'text':answer,'source_ids':['c0s0','c1s0']}]
        result = declared_coverage(answer,claims,self.bank)
        self.assertEqual([row['status'] for row in result],['declared','declared','no_data'])
        self.assertEqual(len(result[0]['claims'][0]['sources']),2)
        self.assertEqual(result[0]['sources'][0]['value'],34.4)

    def test_one_source_can_support_multiple_conclusions(self):
        # 다대다 관계의 반대 방향: 자료 하나를 두 문단에서 사용해도 중복 오류가 아니다.
        result = declared_coverage('첫 결론. 두 번째 결론.',[
            {'text':'첫 결론.','source_ids':['c0s0']},
            {'text':'두 번째 결론.','source_ids':['c0s0']}],self.bank)
        self.assertEqual(len(result[0]['claims']),2)
        self.assertEqual(result[1]['status'],'not_declared')

    def test_empty_declarations_are_not_inferred_by_keywords(self):
        # 본문에 숫자가 있어도 사용 선언이 없으면 서버가 임의로 O를 만들지 않는다.
        result = declared_coverage('RSI 34.4 MACD -2',[],self.bank)
        self.assertEqual(result[0]['status'],'not_declared')

    def test_invalid_ids_or_quotes_are_not_accepted(self):
        # 깨진 반환값은 미명시가 아니라 검증 불가로 구분한다.
        for claims in [None,[{'text':'답변','source_ids':['unknown']}],
                       [{'text':'다른 문장','source_ids':['c0s0']}],
                       [{'text':'답변','source_ids':[]}]]:
            result = declared_coverage('답변',claims,self.bank)
            self.assertEqual(result[0]['status'],'unknown')
            self.assertEqual(result[2]['status'],'no_data')

    def test_no_data_cannot_be_declared(self):
        result = declared_coverage('뉴스 분석',[{'text':'뉴스 분석','source_ids':['c2s0']}],self.bank)
        self.assertEqual(result[2]['status'],'no_data')

    def test_financial_sources_preserve_period_currency_and_zero(self):
        # 실제 0을 누락값으로 오해하지 않고, 분기 항목에 연간 자료가 섞이지 않게 한다.
        research = {'financials':{'status':'partial','currency':'KRW','statements':{
            'yearly':{'income':[{'period_end':'2025-12-31','values':{'Total Revenue':900}}]},
            'quarterly':{'income':[{'period_end':'2026-06-30','values':{'Total Revenue':0,'Operating Income':-10}}]}}}}
        bank = evidence_bank(['분기 매출·영업이익·이익률 변화'],research)
        self.assertEqual(len(bank[0]['sources']),1)
        self.assertEqual(bank[0]['sources'][0]['value']['values']['Total Revenue'],0)
        self.assertEqual(bank[0]['sources'][0]['value']['period_end'],'2026-06-30')

    def test_every_criterion_has_explicit_mapping(self):
        self.assertEqual(set(PATHS), {item for items in RESEARCH_FOCUS.values() for item in items})
        self.assertEqual({key for key, paths in PATHS.items() if 'financials' in paths}, set(FINANCIAL_FIELDS))
        self.assertEqual([len(RESEARCH_FOCUS[horizon]) for horizon in Horizon], [16, 12, 20])
        self.assertIn('가장 최근 완료된 월봉부터 3개 이상 연속', RESEARCH_FOCUS[Horizon.MEDIUM][10])

    def test_short_indicators_have_separate_divergence_evidence(self):
        criteria = RESEARCH_FOCUS[Horizon.SHORT]
        self.assertEqual(criteria[1:7], ('RSI', 'RSI 상승다이버전스', 'RSI 하락다이버전스',
                                       'MACD', 'MACD 상승다이버전스', 'MACD 하락다이버전스'))
        bank = evidence_bank(criteria, {'technicals': {'status': 'partial', 'rsi14': 40,
            'macd_12_26_9': {'line': -2},
            'rsi_divergence': {'bullish': {'status': 'ok', 'detected': True},
                               'bearish': {'status': 'ok', 'detected': False}},
            'macd_divergence': {'bullish': {'status': 'unavailable', 'detected': None, 'reason': '부족'},
                                'bearish': {'status': 'ok', 'detected': False}}}})
        self.assertEqual([item['sources'][0]['path'] if item['sources'] else None for item in bank[1:7]],
                         ['technicals.rsi14', 'technicals.rsi_divergence.bullish', 'technicals.rsi_divergence.bearish',
                          'technicals.macd_12_26_9', None, 'technicals.macd_divergence.bearish'])
        # RSI 상승 판단을 사용해도 RSI 기본값이나 하락 판단을 사용했다고 자동 표시하지 않는다.
        result = declared_coverage('상승 다이버전스 탐지',
                                   [{'text': '상승 다이버전스 탐지', 'source_ids': ['c2s0']}], bank)
        self.assertEqual([row['status'] for row in result[1:7]],
                         ['not_declared', 'declared', 'not_declared', 'not_declared', 'no_data', 'not_declared'])

    def test_reordering_criteria_preserves_their_sources(self):
        research = {'technicals': {'status': 'ok', 'rsi14': 45, 'macd_12_26_9': {'line': -2}}}
        bank = evidence_bank(['MACD', 'RSI'], research)
        self.assertEqual([item['sources'][0]['path'] for item in bank],
                         ['technicals.macd_12_26_9', 'technicals.rsi14'])

    def test_news_does_not_substitute_for_missing_business_and_forecast_data(self):
        criteria = ['고객·제품·지역별 매출 의존도', '사업 구조 및 주요 수익원',
                    '실적 발표 결과와 시장 예상치의 차이', '시장·업종 대비 단기 상대강도']
        bank = evidence_bank(criteria, {'news': {'status': 'ok', 'items': [{'title': '주가 상승'}]},
            'technicals': {'status': 'ok', 'momentum_returns_pct': {'20': 10}}})
        self.assertTrue(all(not item['sources'] and item['limitations'] for item in bank))
        self.assertTrue(all(item['status'] == 'no_data' for item in declared_coverage('자료 부족', [], bank)))

    def test_partial_component_limitations_survive_usage_declaration(self):
        bank = evidence_bank(['변동성 및 ATR'], {'technicals': {
            'status': 'ok', 'volatility_20_annualized_pct': 30}})
        result = declared_coverage('변동성 30%', [{'text': '변동성 30%', 'source_ids': ['c0s0']}], bank)
        self.assertEqual(result[0]['status'], 'declared')
        self.assertIn('ATR 계산값은 미제공', result[0]['limitations'])

    def test_long_growth_uses_annual_rows(self):
        row = {'period_end': '2025-12-31', 'values': {'Net Income': 100}}
        bank = evidence_bank(['장기 매출·영업이익·순이익 성장'], {'financials': {
            'status': 'ok', 'currency': 'USD', 'statements': {
                'yearly': {'income': [row]}, 'quarterly': {'income': [row]}}}})
        self.assertEqual(len(bank[0]['sources']), 1)
        self.assertIn('.yearly.', bank[0]['sources'][0]['path'])

    def test_schema_only_allows_current_source_ids(self):
        schema = answer_schema(self.bank)
        self.assertEqual(schema['properties']['sections']['items']['properties']['source_ids']['items']['enum'], ['c0s0','c1s0'])
        self.assertNotIn('enum',answer_schema([])['properties']['sections']['items']['properties']['source_ids']['items'])

    def test_recent_indicator_windows_are_available_as_separate_evidence(self):
        for status in ('ok', 'partial', 'unavailable'):
            with self.subTest(status=status):
                recent = {'status': status, 'windows': {'20': {'values': {'latest': 45}}}}
                bank = evidence_bank(['RSI', 'MACD'], {'technicals': {
                    'status': 'partial', 'rsi14': 45, 'macd_12_26_9': {'line': -2},
                    'rsi_recent': recent, 'macd_recent': recent}})
                for index, key in enumerate(('rsi_recent', 'macd_recent')):
                    sources = bank[index]['sources']
                    self.assertEqual(len(sources), 1 if status == 'unavailable' else 2)
                    if status != 'unavailable':
                        self.assertEqual(sources[1]['path'], f'technicals.{key}')
                        self.assertEqual(sources[1]['value'], recent)

"""모델이 답변과 함께 선언한 다대다 근거 연결을 검증한다.

자료 준비: research → evidence_bank → 항목별 source ID와 원본 값.
답변 생성: llm.py가 이 목록과 answer_schema를 모델에게 전달한다.
화면 준비: sections의 source_ids → declared_coverage → 항목별 사용 명시 상태.

이 모듈은 OpenAI를 직접 호출하지 않는다. '모델이 사용했다고 명시했다'는 사실을
표시할 뿐, 모델 내부 사고나 근거의 논리적 타당성을 별도 평가하지 않는다.
"""
from copy import deepcopy

# 항목명으로 연결하여 체크리스트 순서가 바뀌어도 근거가 뒤섞이지 않게 한다.
# []는 미수집이다. 뉴스 제목을 사업 구조·매출 의존도의 증거로 대신 넣지 않는다.
PATHS = {
    '최근 주가 차트 및 가격 구조 분석': ['prices.recent_bars', 'technicals.close', 'technicals.moving_averages'],
    'RSI': ['technicals.rsi14', 'technicals.rsi_recent'],
    'RSI 상승다이버전스': ['technicals.rsi_divergence.bullish'],
    'RSI 하락다이버전스': ['technicals.rsi_divergence.bearish'],
    'MACD': ['technicals.macd_12_26_9', 'technicals.macd_recent'],
    'MACD 상승다이버전스': ['technicals.macd_divergence.bullish'],
    'MACD 하락다이버전스': ['technicals.macd_divergence.bearish'],
    '볼린저 밴드 상황': ['technicals.bollinger_20_2'],
    '단기 모멘텀 및 가격 변화율': ['technicals.momentum_returns_pct'],
    '거래량 및 거래대금 변화': ['technicals.volume'],
    '지지·저항 구간': ['technicals.support_resistance_proxy'],
    '변동성 및 ATR': ['technicals.volatility_20_annualized_pct'],
    '시장·업종 대비 단기 상대강도': [],
    '최근 관련 기사 및 주요 뉴스': ['news.items'],
    '가까운 실적 발표·공시 일정': ['events.items'],
    '주요 시나리오별 무효화 가격 및 손익 위험': ['technicals.close', 'technicals.support_resistance_proxy', 'technicals.volatility_20_annualized_pct'],
    '20일·60일·120일·200일 이동평균선 배열 상태': ['technicals.moving_averages', 'technicals.bullish_ma_alignment'],
    '이동평균선별 기울기 및 추세 변화': ['technicals.moving_averages'],
    '주가와 주요 이동평균선 사이의 거리': ['technicals.close', 'technicals.moving_averages'],
    '중기 모멘텀 및 기간별 수익률': ['technicals.momentum_returns_pct'],
    '시장·업종 대비 중기 상대강도': [],
    '분기 매출·영업이익·이익률 변화': ['financials'],
    '실적 발표 결과와 시장 예상치의 차이': [],
    '향후 매출·영업이익 전망 변화': [],
    '업황 및 산업 사이클': ['news.items'],
    '관련 기사 및 주가에 영향을 주는 촉매': ['news.items'],
    '월봉 추세 확인: 가장 최근 완료된 월봉부터 3개 이상 연속으로 '
    '각 종가가 해당 월을 포함한 최근 5개월 종가 평균보다 높은지 확인 '
    '(미완료 월봉 제외, 평균과 종가가 같으면 미충족)': ['technicals.monthly_settlement'],
    '중기 투자 가설의 유효성 및 주요 하방 위험': ['technicals.moving_averages', 'technicals.monthly_settlement', 'financials', 'news.items'],
    '사업 구조 및 주요 수익원': [],
    '기업의 미래 성장 동력과 시장 규모': [],
    '사업 경쟁력 및 지속 가능한 경쟁우위': [],
    '고객·제품·지역별 매출 의존도': [],
    '경쟁·규제·기술 변화에 따른 위험': ['news.items'],
    '장기 매출·영업이익·순이익 성장': ['financials'],
    '영업이익률 및 수익성 추이': ['financials'],
    '영업현금흐름과 잉여현금흐름(FCF)': ['financials'],
    'ROE·ROIC 및 자본 효율성': ['financials', 'valuation.metrics.roe'],
    '부채 부담 및 이자 지급 능력': ['financials'],
    '경영진의 역량과 자본 배분': ['financials'],
    '배당·자사주 매입·투자 지출의 적절성': ['financials'],
    '주식 수 변화와 지분 희석 가능성': ['financials'],
    '실적 PER·선행 PER과 동종업계 비교': ['valuation.metrics.trailing_pe', 'valuation.metrics.forward_pe', 'valuation.benchmark'],
    'PBR과 ROE를 함께 고려한 자산가치 평가': ['valuation.metrics.price_to_book', 'valuation.metrics.roe'],
    'FCF 수익률과 현금 창출력 대비 가격 수준': ['valuation.metrics.fcf_yield', 'financials'],
    '업종에 적합한 경우 EV/EBITDA 비교': ['valuation.metrics.ev_to_ebitda'],
    '주요 가치평가 지표의 과거 범위 대비 현재 위치': ['valuation.historical'],
    '현재 가격에 반영된 성장 기대와 실적 전망': ['financials', 'valuation.metrics.forward_pe'],
    '장기 투자 가설의 핵심 위험': ['financials', 'news.items'],
}

# 일부 자료만으로 복합 항목 전체를 분석했다고 오해하지 않도록 모델과 UI에 공유한다.
# 새 계산/공급원을 구현하면 해당 한계 설명도 함께 갱신한다.
LIMITATIONS = {
    '거래량 및 거래대금 변화': '거래량만 제공하며 실제 거래대금은 미제공입니다.',
    '변동성 및 ATR': '수익률 기반 변동성만 제공하며 ATR 계산값은 미제공입니다.',
    '시장·업종 대비 단기 상대강도': '비교 지수·업종 가격 자료가 미제공입니다.',
    '시장·업종 대비 중기 상대강도': '비교 지수·업종 가격 자료가 미제공입니다.',
    '가까운 실적 발표·공시 일정': '실적 발표 예상일만 조회하며 기타 공시 일정은 미지원입니다.',
    '주요 시나리오별 무효화 가격 및 손익 위험': '가격·변동성은 참고 자료입니다. 진입·목표·무효화 조건은 가정으로 구분해야 합니다.',
    '실적 발표 결과와 시장 예상치의 차이': '같은 회계기간의 실적 컨센서스 비교 자료가 미제공입니다.',
    '향후 매출·영업이익 전망 변화': '회사 가이던스와 시장 추정치의 시점별 변화 자료가 미제공입니다.',
    '업황 및 산업 사이클': '뉴스 제목만 제공하며 업황 통계와 기사 본문은 미제공입니다.',
    '사업 구조 및 주요 수익원': '사업보고서와 사업 부문별 매출 자료가 미제공입니다.',
    '기업의 미래 성장 동력과 시장 규모': '사업계획·시장 규모를 검증할 원문 자료가 미제공입니다.',
    '사업 경쟁력 및 지속 가능한 경쟁우위': '경쟁사 비교와 경쟁우위를 검증할 원문 자료가 미제공입니다.',
    '고객·제품·지역별 매출 의존도': '고객·제품·지역별 매출 구성 자료가 미제공입니다.',
    '경쟁·규제·기술 변화에 따른 위험': '뉴스 제목은 이슈 확인의 단서이며 기사 본문·공시 원문은 미제공입니다.',
    'ROE·ROIC 및 자본 효율성': '공급자 ROE는 제공 시에만 사용합니다. ROIC와 검증된 세후영업이익·투하자본은 미제공입니다.',
    '부채 부담 및 이자 지급 능력': '부채·현금·현금흐름만 제공하며 이자비용과 이자보상배율은 미제공입니다.',
    '경영진의 역량과 자본 배분': '투자지출·현금흐름은 제공하지만 경영진 역량과 자본 배분 정책 원문은 미제공입니다.',
    '배당·자사주 매입·투자 지출의 적절성': '투자지출·현금흐름만 제공하며 배당·자사주 매입 상세 내역은 미제공입니다.',
    '실적 PER·선행 PER과 동종업계 비교': '기업 배수는 공급자 제공 시 사용합니다. 업종 평균 공급원은 미연결이며 선행 PER의 예상 기간도 확인이 필요합니다.',
    'PBR과 ROE를 함께 고려한 자산가치 평가': '공급자 지표이며 장부가치 기준일과 ROE 집계기간을 확인해야 합니다.',
    'FCF 수익률과 현금 창출력 대비 가격 수준': '동일 통화 확인 시 공급자 FCF/시가총액으로 계산합니다. FCF의 정확한 집계기간은 미확인입니다.',
    '업종에 적합한 경우 EV/EBITDA 비교': '기업 배수만 제공하며 업종 비교는 미제공입니다. 금융업 등에는 부적합할 수 있고 설비투자도 따로 확인해야 합니다.',
    '주요 가치평가 지표의 과거 범위 대비 현재 위치': '과거 시점별 배수 자료 미연결. 현재 EPS로 과거 PER을 만들지 않습니다.',
    '현재 가격에 반영된 성장 기대와 실적 전망': '선행 PER이 있어도 시장 기대를 역산하는 모형·추정치 변경 이력·회사 가이던스는 미제공입니다.',
}
FINANCIAL_FIELDS = {
# 전체 재무표를 모든 항목에 복제하지 않고 해당 항목의 후보 필드만 추린다.
# 연간/분기 구분과 period_end/currency는 evidence_bank에서 보존한다.
    '분기 매출·영업이익·이익률 변화': {'Total Revenue', 'Operating Income'},
    '중기 투자 가설의 유효성 및 주요 하방 위험': {'Total Revenue', 'Operating Income', 'Total Debt', 'Operating Cash Flow'},
    '장기 매출·영업이익·순이익 성장': {'Total Revenue', 'Operating Income', 'Net Income', 'Diluted EPS'},
    '영업이익률 및 수익성 추이': {'Total Revenue', 'Operating Income', 'Net Income'},
    '영업현금흐름과 잉여현금흐름(FCF)': {'Operating Cash Flow', 'Free Cash Flow', 'Capital Expenditure'},
    'ROE·ROIC 및 자본 효율성': {'Net Income', 'Stockholders Equity', 'Total Debt', 'Cash And Cash Equivalents'},
    '부채 부담 및 이자 지급 능력': {'Total Debt', 'Total Liabilities', 'Stockholders Equity', 'Total Equity', 'Cash And Cash Equivalents', 'Operating Cash Flow'},
    '경영진의 역량과 자본 배분': {'Capital Expenditure', 'Free Cash Flow'},
    '배당·자사주 매입·투자 지출의 적절성': {'Capital Expenditure', 'Operating Cash Flow', 'Free Cash Flow'},
    'FCF 수익률과 현금 창출력 대비 가격 수준': {'Operating Cash Flow', 'Free Cash Flow', 'Capital Expenditure'},
    '현재 가격에 반영된 성장 기대와 실적 전망': {'Total Revenue', 'Operating Income', 'Operating Cash Flow', 'Capital Expenditure'},
    '장기 투자 가설의 핵심 위험': {'Total Revenue', 'Operating Income', 'Total Debt', 'Operating Cash Flow', 'Free Cash Flow'},
    '주식 수 변화와 지분 희석 가능성': {'Ordinary Shares Number'},
}


def meaningful(value):
    """실제 값이 있는지 검사한다. 0과 False는 값이며 None과 설명문만 있는 객체는 아니다."""
    if isinstance(value, dict):
        return any(meaningful(v) for k, v in value.items() if k not in {'rule', 'method', 'month', 'time'})
    if isinstance(value, list):
        return any(meaningful(v) for v in value)
    return value is not None and value != ''


def evidence_bank(criteria, research):
    """모델에 전달된 자료에서 원본 값·시점을 보존한 근거 후보를 생성한다."""
    # llm.py는 원본 저장소가 아닌 scoped_resources 결과를 넘긴다. 따라서 중기 근거
    # 목록에 장기 전용 기간이 섞이지 않는다. ID는 이 호출의 bank 안에서만 해석한다.
    bank = []
    for index, criterion in enumerate(criteria):
        records = []
        def add(path, value, meta):
            # c8s0은 현재 체크리스트 8번 항목의 0번 자료라는 임시 ID다.
            # 종목에 영구 부여한 ID가 아니므로 다른 턴/관점의 ID를 재사용하면 안 된다.
            # 지표 전체는 partial이어도 개별 다이버전스는 판단 불가일 수 있다.
            # status/reason 문자열만 있는 결과를 실제 분석 근거로 올리지 않는다.
            if isinstance(value, dict) and value.get('status') in {'unavailable', 'empty', 'not_requested'}:
                return
            if meaningful(value):
                records.append({'id': f'c{index}s{len(records)}', 'path': path, 'value': value,
                                'source': meta.get('source', ''), 'as_of': meta.get('as_of', ''),
                                'fetched_at': meta.get('fetched_at', ''), 'status': meta.get('status', '')})
        for path in PATHS.get(criterion, []):
            root = research.get(path.split('.')[0], {})
            if not isinstance(root, dict) or root.get('status') not in {'ok', 'partial'}:
                # unavailable/empty/not_requested는 사용 가능한 값의 증거로 취급하지 않는다.
                continue
            if path == 'financials':
                for frequency, statements in root.get('statements', {}).items():
                    if criterion == '분기 매출·영업이익·이익률 변화' and frequency != 'quarterly':
                        # 연간 실적을 분기 증감의 직접 자료로 연결하는 것을 막는다.
                        continue
                    if criterion == '장기 매출·영업이익·순이익 성장' and frequency != 'yearly':
                        continue
                    for statement, rows in statements.items():
                        for row_index, row in enumerate(rows):
                            values = {k: v for k, v in row.get('values', {}).items()
                                      if k in FINANCIAL_FIELDS.get(criterion, set()) and v is not None}
                            if values:
                                add(f'financials.statements.{frequency}.{statement}.{row_index}',
                                    {'period_end': row.get('period_end'), 'currency': root.get('currency'),
                                     **{key: row[key] for key in ('scope', 'source_urls', 'value_basis') if key in row},
                                     'values': values}, root)
            else:
                value = root
                for part in path.split('.')[1:]:
                    value = value.get(part) if isinstance(value, dict) else None
                if isinstance(value, list):
                    for row_index, item in enumerate(value):
                        add(f'{path}.{row_index}', item, root)
                else:
                    add(path, value, root)
        bank.append({'id': index, 'criterion': criterion, 'sources': records,
                     'limitations': LIMITATIONS.get(criterion, '')})
    return bank


ANSWER_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['sections'],
    'properties': {'sections': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['text', 'source_ids'],
        'properties': {'text': {'type': 'string'}, 'source_ids': {'type': 'array', 'items': {'type': 'string'}}}}}}}

# 아래 문자열은 개발자 설명 주석이 아니라 실제 모델 지시문이다.
# 학습용 설명을 이 문자열에 추가하면 모델 동작도 바뀌므로 바깥 주석으로 설명한다.
ANSWER_INSTRUCTIONS = """최종 출력은 sections 배열을 담은 JSON이다.
각 section.text에 사용자에게 보여줄 분석 문단 또는 결론을 작성한다. 화면에는 이 문단들을 순서대로 보여준다.
각 문단을 작성하면서 실제 근거로 사용한 자료 연결을 같은 section에 함께 선언한다. 사후 평가를 작성하지 않는다.
source_ids에는 이번 호출의 근거 목록에서 그 문장에 사용한 모든 ID를 넣는다.
여러 지표·재무·뉴스를 종합한 결론에는 여러 source_ids를 연결한다. 같은 자료를 여러 문장에 연결해도 된다.
원본 수치를 답변에 그대로 반복할 필요는 없다. 자료를 종합한 해석이나 결론도 연결할 수 있다.
근거가 없는 일반 설명·기간 확인 문단에는 source_ids를 빈 배열로 한다. 사용하지 않은 자료를 억지로 연결하지 않는다.
이전 호출의 ID는 사용하지 않는다. 이번 근거 목록이 비어 있으면 모든 source_ids도 빈 배열이다.
뉴스는 제목만 제공된 자료다. 본문을 읽었다고 주장하지 않는다. 누락된 기간이나 수치는 만들어내지 않는다.
"""


def answer_schema(bank):
    """응답 JSON 구조를 복사하고 이번 호출의 source ID만 enum으로 허용한다."""
    schema = deepcopy(ANSWER_SCHEMA)
    ids = [source['id'] for item in bank for source in item['sources']]
    if ids:
        schema['properties']['sections']['items']['properties']['source_ids']['items']['enum'] = ids
    return schema


def defaults(bank):
    """검증 전 기본 상태. 원본 자료 없음(no_data)과 연결 검증 불가(unknown)를 분리한다."""
    return [{'criterion': item['criterion'], 'status': 'unknown' if item['sources'] else 'no_data',
             'evidence': '', 'sources': [], 'available_sources': item['sources'],
             'limitations': item.get('limitations', ''),
             'claims': [],
             'reason': '모델이 반환한 근거 연결을 검증하지 못했습니다.' if item['sources'] else '이번 조사에서 이 항목을 뒷받침할 자료가 제공되지 않았습니다.'}
            for item in bank]


def declared_coverage(answer, claims, bank):
    """하나의 결론에 여러 자료를 연결한다. 의미·사실성 판정 대신 ID와 문장 존재만 검증한다."""
    results = defaults(bank)
    if not isinstance(claims, list):
        return results
    allowed = {source['id']: source for item in bank for source in item['sources']}
    # 먼저 전체 선언을 검증한다. 하나라도 없는 ID/문장이라면 불완전한 결과를
    # '미사용'으로 오해하지 않도록 기본 unknown 상태를 반환한다.
    for claim in claims:
        if not isinstance(claim, dict):
            return results
        text, ids = claim.get('text'), claim.get('source_ids')
        if (not isinstance(text, str) or not text.strip() or text not in answer
                or not isinstance(ids, list) or not ids
                or any(not isinstance(key, str) or key not in allowed for key in ids)):
            return results
    for index, item in enumerate(bank):
        # 결론 하나가 RSI와 MACD를 함께 연결하면 양쪽 항목 모두 그 결론을 참조한다.
        # sources는 현재 항목 자료만, claims[*].sources는 결론에 연결된 전체 자료다.
        # 프론트는 중복을 줄이기 위해 현재 항목의 sources만 화면에 표시한다.
        if not item['sources']:
            continue
        own_ids = {source['id'] for source in item['sources']}
        linked = [claim for claim in claims if own_ids.intersection(claim['source_ids'])]
        ids = {key for claim in linked for key in claim['source_ids'] if key in own_ids}
        results[index].update(
            status='declared' if linked else 'not_declared',
            reason='답변 생성 모델이 이 자료를 분석·결론의 근거로 명시했습니다.' if linked else '자료는 제공됐으나 답변 생성 모델이 사용 근거로 명시하지 않았습니다.',
            sources=[source for source in item['sources'] if source['id'] in ids],
            claims=[{'text': claim['text'], 'sources': [allowed[key] for key in dict.fromkeys(claim['source_ids'])]} for claim in linked],
        )
    return results

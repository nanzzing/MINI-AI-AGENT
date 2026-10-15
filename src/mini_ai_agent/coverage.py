"""모델이 답변과 함께 선언한 다대다 근거 연결을 검증한다.

자료 준비: research → evidence_bank → 항목별 source ID와 원본 값.
답변 생성: llm.py가 이 목록과 answer_schema를 모델에게 전달한다.
화면 준비: sections의 source_ids → declared_coverage → 항목별 사용 명시 상태.

이 모듈은 OpenAI를 직접 호출하지 않는다. '모델이 사용했다고 명시했다'는 사실을
표시할 뿐, 모델 내부 사고나 근거의 논리적 타당성을 별도 평가하지 않는다.
"""
from copy import deepcopy
from mini_ai_agent.conversation import Horizon, RESEARCH_FOCUS

# 항목별 실제 제공 자료만 허용한다. 아직 수집하지 않는 자료는 빈 목록이다.
SHORT_PATHS = [
    ['technicals.rsi14'],
    ['technicals.rsi_divergence.bullish'], ['technicals.rsi_divergence.bearish'],
    ['technicals.macd_12_26_9'],
    ['technicals.macd_divergence.bullish'], ['technicals.macd_divergence.bearish'],
    ['prices.recent_bars', 'technicals.close', 'technicals.moving_averages'],
    ['technicals.momentum_returns_pct'], ['news.items'], ['technicals.volume'],
    ['technicals.support_resistance_proxy'], ['technicals.volatility_20_annualized_pct'],
    ['events.items'], ['technicals.bollinger_20_2'],
]
MEDIUM_PATHS = [
    # 각 안쪽 리스트는 '중기 체크리스트 한 항목'이 참조할 수 있는 후보 자료 경로다.
    # 같은 moving_averages가 여러 항목에 반복되는 것은 정배열/기울기/이격률이
    # 같은 원자료를 공유하기 때문이다. []는 아직 해당 비교 데이터가 없다는 뜻이다.
    ['technicals.moving_averages', 'technicals.bullish_ma_alignment'],
    ['technicals.momentum_returns_pct'], ['news.items'], ['technicals.moving_averages'],
    ['technicals.close', 'technicals.moving_averages'], ['financials'], ['news.items'], [],
    ['technicals.monthly_settlement'],
]
LONG_PATHS = [['news.items'], ['news.items'], ['financials'], ['financials'], ['financials'],
              ['news.items', 'financials'], ['technicals.close', 'financials'], ['financials', 'news.items'],
              ['news.items'], ['financials'], ['news.items']]
PATHS = dict(zip(RESEARCH_FOCUS[Horizon.SHORT], SHORT_PATHS))
# 각 PATHS 목록은 conversation.py의 기준 순서와 대응한다. 체크리스트 항목을
# 추가/이동할 때 여기도 같이 확인해야 한다. 경로는 research dict 안의 위치다.
# 예: technicals.rsi14 → research['technicals']['rsi14'].
# 뉴스가 후보라는 이유만으로 고객 의존도 등을 입증하는 뉴스라는 뜻은 아니다.
PATHS.update(zip(RESEARCH_FOCUS[Horizon.MEDIUM], MEDIUM_PATHS))
PATHS.update(zip(RESEARCH_FOCUS[Horizon.LONG], LONG_PATHS))
FINANCIAL_FIELDS = {
# 전체 재무표를 모든 항목에 복제하지 않고 해당 항목의 후보 필드만 추린다.
# 연간/분기 구분과 period_end/currency는 evidence_bank에서 보존한다.
    '분기 매출·영업이익 변화': {'Total Revenue', 'Operating Income'},
    '매출·이익 성장': {'Total Revenue', 'Operating Income', 'Net Income', 'Diluted EPS'},
    '현금흐름': {'Operating Cash Flow', 'Free Cash Flow', 'Capital Expenditure'},
    '부채 부담': {'Total Debt', 'Stockholders Equity', 'Cash And Cash Equivalents', 'Operating Cash Flow'},
    '경영진과 자본 배분': {'Capital Expenditure', 'Free Cash Flow'},
    '현재 가격의 적정성': {'Diluted EPS', 'Stockholders Equity', 'Ordinary Shares Number', 'Net Income'},
    '성장의 지속 가능성': {'Total Revenue', 'Operating Income', 'Operating Cash Flow', 'Capital Expenditure'},
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
                    if criterion == '분기 매출·영업이익 변화' and frequency != 'quarterly':
                        # 연간 실적을 분기 증감의 직접 자료로 연결하는 것을 막는다.
                        continue
                    for statement, rows in statements.items():
                        for row_index, row in enumerate(rows):
                            values = {k: v for k, v in row.get('values', {}).items()
                                      if k in FINANCIAL_FIELDS.get(criterion, set()) and v is not None}
                            if values:
                                add(f'financials.statements.{frequency}.{statement}.{row_index}',
                                    {'period_end': row.get('period_end'), 'currency': root.get('currency'),
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
        bank.append({'id': index, 'criterion': criterion, 'sources': records})
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

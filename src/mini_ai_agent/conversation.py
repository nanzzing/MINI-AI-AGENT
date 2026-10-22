"""투자 기간·조사 기준·상태 정의와 초기 규칙 기반 대화 예제.

현재 웹과 CLI는 llm.py의 OpenAIConversation을 사용한다. 이 파일의 공통 정의는
그곳에서도 사용하지만, InvestmentConversation은 모델 없이 흐름을 확인하는 예제다.
"""

from dataclasses import dataclass
from enum import Enum
from mini_ai_agent.stocks import find_stocks


class Horizon(str, Enum):
    """문자열로 전달할 수 있는 투자 기간. '중기'도 내부에서는 '중장기'로 통일한다."""
    SHORT = "단기"
    MEDIUM = "중장기"
    LONG = "장기"


# 모델 지시문과 화면 체크리스트가 함께 사용하는 조사 기준이다. 지표 계산 결과는 아니다.
RESEARCH_FOCUS = {
    # 모델의 조사 지침과 화면 체크리스트가 공통으로 사용하는 단일 기준표다.
    # 항목이 여기 존재해도 실제 수집기가 구현됐다는 뜻은 아니다. coverage.py에서
    # 항목별 제공 자료를 연결하며, 예를 들어 업종 비교 자료는 현재 미구현이다.
    Horizon.SHORT: (
        "최근 주가 차트 및 가격 구조 분석",
        "RSI",
        "RSI 상승다이버전스",
        "RSI 하락다이버전스",
        "MACD",
        "MACD 상승다이버전스",
        "MACD 하락다이버전스",
        "볼린저 밴드 상황",
        "단기 모멘텀 및 가격 변화율",
        "거래량 및 거래대금 변화",
        "지지·저항 구간",
        "변동성 및 ATR",
        "시장·업종 대비 단기 상대강도",
        "최근 관련 기사 및 주요 뉴스",
        "가까운 실적 발표·공시 일정",
        "주요 시나리오별 무효화 가격 및 손익 위험",
    ),
    Horizon.MEDIUM: (
        "20일·60일·120일·200일 이동평균선 배열 상태",
        "이동평균선별 기울기 및 추세 변화",
        "주가와 주요 이동평균선 사이의 거리",
        "중기 모멘텀 및 기간별 수익률",
        "시장·업종 대비 중기 상대강도",
        "분기 매출·영업이익·이익률 변화",
        "실적 발표 결과와 시장 예상치의 차이",
        "향후 매출·영업이익 전망 변화",
        "업황 및 산업 사이클",
        "관련 기사 및 주가에 영향을 주는 촉매",
        "월봉 추세 확인: 가장 최근 완료된 월봉부터 3개 이상 연속으로 "
        "각 종가가 해당 월을 포함한 최근 5개월 종가 평균보다 높은지 확인 "
        "(미완료 월봉 제외, 평균과 종가가 같으면 미충족)",
        "중기 투자 가설의 유효성 및 주요 하방 위험",
    ),
    Horizon.LONG: (
        "사업 구조 및 주요 수익원",
        "기업의 미래 성장 동력과 시장 규모",
        "사업 경쟁력 및 지속 가능한 경쟁우위",
        "고객·제품·지역별 매출 의존도",
        "경쟁·규제·기술 변화에 따른 위험",
        "장기 매출·영업이익·순이익 성장",
        "영업이익률 및 수익성 추이",
        "영업현금흐름과 잉여현금흐름(FCF)",
        "ROE·ROIC 및 자본 효율성",
        "부채 부담 및 이자 지급 능력",
        "경영진의 역량과 자본 배분",
        "배당·자사주 매입·투자 지출의 적절성",
        "주식 수 변화와 지분 희석 가능성",
        "실적 PER·선행 PER과 동종업계 비교",
        "PBR과 ROE를 함께 고려한 자산가치 평가",
        "FCF 수익률과 현금 창출력 대비 가격 수준",
        "업종에 적합한 경우 EV/EBITDA 비교",
        "주요 가치평가 지표의 과거 범위 대비 현재 위치",
        "현재 가격에 반영된 성장 기대와 실적 전망",
        "장기 투자 가설의 핵심 위험",
    ),
}


@dataclass
class ConversationState:
    """대화에서 확정한 정보. None은 아직 미정이며 stage로 다음 확인 단계를 나타낸다."""
    company: str | None = None
    code: str | None = None
    exchange: str | None = None
    horizon: Horizon | None = None
    stage: str = "collecting_context"


def detect_horizon(message: str) -> Horizon | None:
    """명시된 관점 단어 하나만 인식하는 보조 규칙. LLM의 자연어 이해를 대체하지 않는다."""
    # '중장기' 안의 '장기'를 중복 인식하지 않도록 먼저 표현을 통일한다.
    message = message.replace("중장기", "중기")
    labels = {Horizon.SHORT: "단기", Horizon.MEDIUM: "중기", Horizon.LONG: "장기"}
    matches = [horizon for horizon, label in labels.items() if label in message]
    return matches[0] if len(matches) == 1 else None  # 복수 기간이 나오면 임의로 하나를 선택하지 않는다.


def detect_company(message: str) -> str | None:
    """검색 결과가 하나일 때만 종목명을 반환하는 규칙 기반 보조 함수."""
    matches = find_stocks(message, allow_indicator_tickers=False)
    return matches[0]["name"] if len(matches) == 1 else None


class InvestmentConversation:
    """API 호출 없이 종목 확인 → 기간 확인 → 조사 항목 안내를 시험하는 대화 객체."""
    def __init__(self) -> None:
        self.state = ConversationState()

    def respond(self, message: str) -> str:
        """초기 학습용 규칙 대화. 실제 웹 요청은 llm.OpenAIConversation.respond로 간다."""
        message = message.strip()
        if not message:
            return "조사할 종목이나 투자 기간을 입력해 주세요."
        if message in {"초기화", "/reset"}:
            self.state = ConversationState()
            return "새로운 조사를 시작할게요. 어떤 종목이 궁금한가요?"

        # 새 입력에 있는 정보만 갱신한다. '장기'만 답해도 이전 종목은 유지된다.
        matches = find_stocks(message, allow_indicator_tickers=False)
        horizon = detect_horizon(message)
        if horizon:
            self.state.horizon = horizon
        if len(matches) > 1:
            # 종목이 모호해지면 이전 종목을 지우고 선택을 요청한다.
            self.state.company = self.state.code = self.state.exchange = None
            self.state.stage = "awaiting_company"
            options = "\n".join(f"- {row['name']} / {row['code']} / {row['exchange']}" for row in matches[:10])
            return f"여러 종목이 확인돼요. 조사할 종목의 코드를 알려주세요.\n{options}"
        if matches:
            stock = matches[0]
            self.state.company = stock["name"]
            self.state.code = stock["code"]
            self.state.exchange = stock["exchange"]

        # 종목이 먼저 확정되어야 기간 질문으로 넘어간다.
        if not self.state.company:
            self.state.stage = "awaiting_company"
            return "어떤 종목을 조사할까요? 한국·미국 주식의 종목명 또는 코드(예: 삼성전자, AAPL)를 입력해 주세요."
        if not self.state.horizon:
            self.state.stage = "awaiting_horizon"
            return (
                f"{self.state.company}를 단기, 중장기, 장기 중 어떤 관점으로 보고 계신가요? "
                "투자 기간을 알려주시면 더 명확하게 답변드릴 수 있어요."
            )

        # 이 단계는 조사 준비 완료를 뜻하며 실제 데이터 분석 완료를 의미하지 않는다.
        self.state.stage = "ready_for_research"
        focus = "\n".join(f"- {item}" for item in RESEARCH_FOCUS[self.state.horizon])
        return (
            f"{self.state.company}를 {self.state.horizon.value} 관점으로 조사할게요.\n\n"
            f"확인할 항목:\n{focus}\n\n"
            "다음 단계는 실제 자료를 수집한 뒤, 근거와 위험 요인을 바탕으로 의견을 정리하는 것이에요. "
            "현재는 데이터 조회와 AI 분석이 연결되지 않아 추천 판단은 아직 제공하지 않아요."
        )

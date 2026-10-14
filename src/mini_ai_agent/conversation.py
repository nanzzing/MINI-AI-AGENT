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
    Horizon.SHORT: (
        "RSI",
        "MACD",
        "최근 주가 차트 분석",
        "모멘텀",
        "최근 기사",
        "거래량",
        "지지·저항 구간",
        "변동성",
        "가까운 실적 발표·공시 일정",
        "볼린저 밴드 상황",
    ),
    Horizon.MEDIUM: (
        "이동평균선 4개 정배열: 20일선 > 60일선 > 120일선 > 200일선",
        "모멘텀",
        "관련 기사",
        "이동평균선 기울기",
        "주가와 이평선의 거리",
        "분기 매출·영업이익 변화",
        "업황",
        "시장·업종 대비 주가 흐름",
        "월봉 안착: 최근 완료된 월봉 3개 이상 연속으로 각 종가가 해당 월의 "
        "5개월 단순이동평균선 위에 있는지 확인 "
        "(해당 월 포함 최근 5개월 종가 평균, 미완료 월봉 제외, 같으면 미충족)",
    ),
    Horizon.LONG: (
        "기업의 미래 유망성",
        "사업 경쟁력",
        "매출·이익 성장",
        "현금흐름",
        "부채 부담",
        "경영진과 자본 배분",
        "현재 가격의 적정성",
        "성장의 지속 가능성",
        "고객·제품 의존도",
        "주식 수 변화와 지분 희석 가능성",
        "경쟁·규제·기술 변화에 따른 위험",
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
    # '중장기' 안의 '장기'를 중복 인식하지 않도록 먼저 표현을 통일한다.
    message = message.replace("중장기", "중기")
    labels = {Horizon.SHORT: "단기", Horizon.MEDIUM: "중기", Horizon.LONG: "장기"}
    matches = [horizon for horizon, label in labels.items() if label in message]
    return matches[0] if len(matches) == 1 else None  # 복수 기간이 나오면 임의로 하나를 선택하지 않는다.


def detect_company(message: str) -> str | None:
    """검색 결과가 하나일 때만 종목명을 반환하는 규칙 기반 보조 함수."""
    matches = find_stocks(message)
    return matches[0]["name"] if len(matches) == 1 else None


class InvestmentConversation:
    """API 호출 없이 종목 확인 → 기간 확인 → 조사 항목 안내를 시험하는 대화 객체."""
    def __init__(self) -> None:
        self.state = ConversationState()

    def respond(self, message: str) -> str:
        message = message.strip()
        if not message:
            return "조사할 종목이나 투자 기간을 입력해 주세요."
        if message in {"초기화", "/reset"}:
            self.state = ConversationState()
            return "새로운 조사를 시작할게요. 어떤 종목이 궁금한가요?"

        # 새 입력에 있는 정보만 갱신한다. '장기'만 답해도 이전 종목은 유지된다.
        matches = find_stocks(message)
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

"""모델 대화의 중심: 사용자 입력 → 모델의 도구 요청 → 실행 결과 전달 → 답변.

server.py와 main.py가 이 모듈을 사용한다. 모델은 실행할 도구를 선택하고,
실제 종목 검색과 상태 변경은 아래 execute_tool에서 파이썬이 수행한다.

학습할 때 구분할 세 경계:
1. TOOLS는 모델에게 보여줄 '설명서', execute_tool은 서버의 실제 실행 코드다.
2. saved_resources는 서버 원본, scoped_resources의 반환값은 이번 관점에 공개할 자료다.
3. tool_calls는 도구별 인자/결과, agent_steps는 모델 호출까지 포함한 실행 순서다.
   둘 다 실행 기록이며 모델 내부 추론을 관찰한 기록은 아니다.

respond의 읽기 순서: 상태 복사 → 문맥 구성 → API 호출 → function_call 판별 →
실행 결과 추가 → 다음 API 호출 → 필수 자료/응답 검증 → 성공 상태 저장.
프롬프트는 행동을 유도하고, 파이썬 검증은 허용 범위와 종료 조건을 강제한다.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path
from threading import Lock
from time import perf_counter

from dotenv import dotenv_values
from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError

from mini_ai_agent.conversation import ConversationState, Horizon, RESEARCH_FOCUS, detect_horizon
from mini_ai_agent.stocks import find_stocks, load_catalog
from mini_ai_agent.market_data import financials, financial_summary, stock_identity, price_history, news, earnings_events, policy
from mini_ai_agent.technicals import analyze
from mini_ai_agent.coverage import evidence_bank, declared_coverage, answer_schema, ANSWER_INSTRUCTIONS

ROOT = Path(__file__).resolve().parents[2]
# 설치 위치가 src/mini_ai_agent일 때 프로젝트 루트의 .env를 읽는다.
# Render의 환경변수는 settings()에서 .env보다 먼저 적용된다.
PROMPT = Path(__file__).parent / "prompts" / "research.txt"


class ChatError(Exception):
    """A user-safe message, never a raw provider error containing credentials."""


def settings() -> tuple[str, str]:
    """요청마다 설정을 읽어 .env 변경을 반영한다. 환경변수가 파일보다 우선한다."""
    values = dotenv_values(ROOT / ".env")
    key = os.environ.get("OPENAI_API_KEY", values.get("OPENAI_API_KEY") or "")
    model = os.environ.get("OPENAI_MODEL", values.get("OPENAI_MODEL") or "gpt-4.1-mini")
    return key.strip(), model.strip()


def configuration_status() -> dict:
    # 화면에는 키 자체를 보내지 않는다. configured는 키 유효성 검증 결과가 아니다.
    key, model = settings()
    return {"configured": bool(key and key != "aaa"), "model": model}


# 모델에게 보내는 도구 설명서다. 함수 구현이나 전체 종목 목록은 여기에 포함하지 않는다.
# strict와 JSON Schema로 입력 형태를 지정하고, 실행 시에도 별도로 검증한다.
TOOLS = [
    *[{"type": "function", "name": name, "description": description,
       "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True}
      for name, description in [
          ("get_price_analysis", "현재 종목·관점의 가격 이력 요약과 파이썬이 계산한 기술적 지표를 조회합니다. RSI·MACD와 각각의 상승·하락 다이버전스 판정 및 비교 전환점을 포함합니다."),
          ("get_news", "현재 종목·관점에 맞는 최근 뉴스 제목과 출처를 조회합니다. 모든 관점에서 필수입니다."),
          ("get_events", "단기 관점의 향후 실적 발표 예상 일정을 조회합니다. 중기·장기에는 호출하지 마세요."),
      ]],
    {"type": "function", "name": "get_financials",
     "description": "현재 조사 종목의 재무제표를 투자 기간별 범위로 조회합니다. 실적·성장·부채·가치 분석 시 사용하세요.",
     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "search_stock",
     "description": "한국·미국 종목 목록에서 종목명·별칭·티커로 검색합니다. 주가나 재무 자료는 반환하지 않습니다.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "set_research_context",
     "description": "검색으로 확인한 종목과 사용자가 선택한 투자 기간을 저장합니다. 기간 미정은 null입니다.",
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string"}, "exchange": {"type": "string"},
         "horizon": {"type": ["string", "null"], "enum": ["단기", "중기", "중장기", "장기", None]}},
         "required": ["code", "exchange", "horizon"], "additionalProperties": False}, "strict": True},
]


class OpenAIConversation:
    """사용자 세션 하나의 조사 상태, 대화 이력, 최근 도구 실행 내역을 보관한다."""

    def __init__(self, client=None):
        self.state = ConversationState()
        self.history: list[dict] = []
        self.tool_calls: list[dict] = []
        self.agent_steps: list[dict] = []
        self.research: dict = {}
        self.saved_resources: dict = {}  # 원본 보관용. API 응답이나 모델 입력에는 직접 넣지 않는다.
        self.coverage: list[dict] = []
        # state: 종목/관점, history: 모델에게 재전송할 메시지와 도구 결과,
        # tool_calls: 이번 답변의 실제 모델 요청 내역, research: 이번 조회 자료,
        # coverage: 같은 모델 응답에서 선언한 자료 사용 관계. 서로 다른 목적의 저장소다.
        # 테스트에서는 가짜 응답을 주는 클라이언트를 주입해 실제 API 비용을 피한다.
        self.client = client
        self.lock = Lock()

    def ensure_financials(self, state: ConversationState, resources: dict) -> dict:
        """get_financials 실행에서 재무 요약과 보관용 원본을 만든다.

        이름이 ensure라도 서버가 매 질문마다 자동 호출하는 함수는 아니다.
        현재 execute_tool은 명시적 조회 요청 때 context_key를 제거하고 이 함수를
        부른다. 실제 네트워크 재조회 여부는 market_data의 TTL 캐시가 결정한다.
        """
        context_key = [state.code, state.exchange, state.horizon.value]
        # 함수 호출자는 종목과 관점이 모두 확정되었음을 보장한다.
        # 종목만 비교하면 중기 자료를 장기 자료로 잘못 재사용하므로 관점까지 키에 넣는다.
        if resources.get("financials_context_key") != context_key:
            result = financials(stock_identity(state.code, state.exchange), state.horizon.value)
            resources["financials"] = financial_summary(result)
            resources["_financials_original"] = deepcopy(resources["financials"])
            resources["financials_context_key"] = context_key
            # unavailable/partial도 '조회 결과'다. 자료가 없는 공급자에 무한 재조회하지 않는다.
            # 다른 턴의 재사용은 market_data.py의 만료 시간 있는 캐시가 담당한다.
        return resources["financials"]

    @staticmethod
    def scoped_resources(state, resources):
        """원본은 유지하고 모델·근거 목록·화면용 재무 기간만 잘라낸다.

        입력: 현재 ConversationState와 서버가 보관한 resources 사전.
        출력: 내부용 '_' 키를 제외한 새로운 사전. 원본은 수정하지 않는다.
        장기 → 중기 → 장기로 돌아와도 원본의 5개년이 남도록 deepcopy한다.
        이는 날짜 범위가 아니라 제공된 최신 회계기간 개수 기준의 선택이다.
        """
        view = deepcopy({key: value for key, value in resources.items() if not key.startswith("_")})
        original = resources.get("_financials_original", resources.get("financials"))
        if not state.horizon or "financials" not in resources or original is None:
            return view
        data = deepcopy(original)
        rules = policy(state.horizon.value)
        statements, missing = {}, []
        for frequency, count in [("yearly", rules["annual_years"]), ("quarterly", rules["quarters"])]:
            statements[frequency] = {}
            for name in ("income", "balance", "cashflow"):
                rows = original.get("statements", {}).get(frequency, {}).get(name, [])
                # 공급자 순서에 의존하지 않고 최신 회계기간부터 선택한다.
                selected = sorted(rows, key=lambda row: row.get("period_end", ""), reverse=True)[:count]
                statements[frequency][name] = selected
                if len(selected) < count:
                    missing.append(f"{frequency}/{name}: 요청 {count}기간, 제공 {len(selected)}기간")
        available = any(rows for section in statements.values() for rows in section.values())
        # 원본이 partial이어도 좁아진 범위를 모두 충족하면 이번 view는 ok일 수 있다.
        # ok는 기간 개수에 대한 판정이며 모든 필드의 존재/수치 정확성까지 보장하지 않는다.
        data.update(statements=statements, missing=missing,
                    requested={"annual_years": rules["annual_years"], "quarters": rules["quarters"]},
                    status="partial" if available and missing else "ok" if available else "unavailable")
        view["financials"] = data
        view["financials_context_key"] = [state.code, state.exchange, state.horizon.value]
        return view

    def reusable_tools(self, state, resources):
        """보유 자료의 재사용 자격만 검사한다. 실제 활용 여부는 모델이 결정한다.

        범위는 실제 조회 관점에 묶어 보관하며 관점 변경으로 부풀리지 않는다.
        뉴스는 제목 10개만 보유하므로 다른 조회 기간을 완전히 커버한다고 가정하지 않는다.
        """
        if not state.horizon:
            return []
        rules = policy(state.horizon.value)
        eligible = []
        # inventory의 policy는 '조회 요청 범위'다. partial 자료도 재사용 후보가 된다.
        # 실제 확보 기간의 부족은 scoped_resources가 다시 계산하여 모델에 알린다.
        # 따라서 eligible이라는 사실을 완전한 데이터가 있다는 뜻으로 해석하면 안 된다.
        for name, record in resources.get("data_inventory", {}).items():
            if record["identity"] != [state.code, state.exchange]:
                continue
            age = datetime.now(timezone.utc).timestamp() - record["timestamp"]
            # 재무는 1시간, 나머지는 5분의 재사용 한도다. fetched_at은 조회 시각이지
            # 주가 거래 시각이나 재무제표 결산일이 아니다. 각 자료의 기준일도 확인해야 한다.
            if age < 0 or age > (3600 if name == "get_financials" else 300):
                continue
            old = record["policy"]
            if name == "get_financials" and (old["annual_years"] < rules["annual_years"] or old["quarters"] < rules["quarters"]):
                continue
            if name == "get_price_analysis" and int(old["price_period"][:-1]) < int(rules["price_period"][:-1]):
                continue
            if name == "get_news" and old["news_days"] != rules["news_days"]:
                continue
            if name == "get_events" and state.horizon != Horizon.SHORT:
                continue
            eligible.append(name)
        return eligible

    def prune_resources(self, state, resources):
        """유효하지 않은 자료는 현재 근거 목록에서도 제거한다."""
        # 이번 턴에 시도했지만 실패한 자료는 남겨 모델에게 '실패했다'는 사실을 전달한다.
        # 다음 턴에는 attempted_tools를 비우므로 실패 결과를 유효한 캐시로 승격하지 않는다.
        allowed = set(self.reusable_tools(state, resources)) | set(resources.get("attempted_tools", []))
        fields = {"get_price_analysis": ["prices", "technicals"], "get_financials": ["financials", "financials_context_key"],
                  "get_news": ["news"], "get_events": ["events"]}
        for name, keys in fields.items():
            if name not in allowed:
                for key in keys:
                    resources.pop(key, None)
                resources.get("data_inventory", {}).pop(name, None)
        resources["reusable_tools"] = self.reusable_tools(state, resources)

    def execute_tool(self, name: str, arguments: str, state: ConversationState, resources=None) -> dict:
        """모델이 보낸 JSON 인자를 검사하고 허용한 도구만 실행한다."""
        resources = resources if resources is not None else {}
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError
            if name in {"get_financials", "get_price_analysis", "get_news", "get_events"}:
                if args:
                    raise ValueError
                if not state.code or not state.horizon:
                    return {"error": "먼저 종목과 투자 기간을 set_research_context로 확정하세요."}
                stock = stock_identity(state.code, state.exchange)
                if name == "get_events" and state.horizon != Horizon.SHORT:
                    return {"error": "일정은 단기 관점에서만 조회합니다."}
                if name == "get_price_analysis":
                    prices = price_history(stock, state.horizon.value)
                    summary = {key: value for key, value in prices.items() if key != "bars"}
                    summary["recent_bars"] = prices.get("bars", [])[-20:]
                    result = {"prices": summary, "technicals": analyze(prices)}
                    resources.update(result)
                elif name == "get_news":
                    result = resources["news"] = news(stock, state.horizon.value)
                elif name == "get_events":
                    result = resources["events"] = earnings_events(stock, state.horizon.value)
                else:
                    # 명시적 조회 요청은 보유 요약을 우회한다. 공급자 캐시는 별도 정책이다.
                    resources.pop("financials_context_key", None)
                    result = self.ensure_financials(state, resources)
                # 조회 실패/부분 제공도 실행 완료로 센다. 없는 자료를 무한 재조회하지 않는다.
                resources.setdefault("attempted_tools", []).append(name)
                source = result.get("prices", result)
                # 가격 도구는 prices/technicals 묶음, 나머지는 단일 결과 객체다.
                # 외부 캐시가 반환한 원래 fetched_at을 유지해 재사용 시간을 연장하지 않는다.
                try:
                    timestamp = datetime.fromisoformat(source["fetched_at"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    timestamp = datetime.now(timezone.utc).timestamp()
                if source.get("status") in {"ok", "partial"}:
                    resources.setdefault("data_inventory", {})[name] = {
                        "identity": [state.code, state.exchange], "policy": deepcopy(policy(state.horizon.value)),
                        "timestamp": timestamp, "status": source["status"]}
                else:
                    resources.get("data_inventory", {}).pop(name, None)
                return result
            if name == "search_stock":
                query = args.get("query")
                if not isinstance(query, str) or not 1 <= len(query) <= 200:
                    raise ValueError
                # 로컬 목록을 검색해 필요한 결과만 돌려주므로 전체 목록을 모델에 보낼 필요가 없다.
                return {"stocks": find_stocks(query)[:20], "source": "KIS stock directory",
                        "fetched_at": load_catalog()["fetched_at"]}
            if name == "set_research_context":
                # 모델이 만든 코드·거래소를 그대로 믿지 않고 실제 목록과 대조한다.
                matches = [row for row in load_catalog()["stocks"]
                           if row["code"] == args.get("code") and row["exchange"] == args.get("exchange")]
                if len(matches) != 1:
                    return {"error": "해당 종목을 확인할 수 없습니다. search_stock으로 다시 검색하세요."}
                horizon = args.get("horizon")
                if horizon == "중기":
                    horizon = "중장기"
                selected = Horizon(horizon) if horizon is not None else None
                stock = matches[0]
                same_context = (state.code == stock["code"] and state.exchange == stock["exchange"] and state.horizon == selected)
                # 같은 종목의 관점 변경은 원자료를 활용할 수 있지만, 종목 변경은 전부 비운다.
                # 이 구현은 여러 종목을 보관하는 포트폴리오 캐시가 아니라 현재 종목용 메모리다.
                same_stock = state.code == stock["code"] and state.exchange == stock["exchange"]
                state.company, state.code, state.exchange = stock["name"], stock["code"], stock["exchange"]
                state.horizon = selected
                state.stage = "ready_for_research" if selected else "awaiting_horizon"
                if not same_stock:
                    resources.clear()
                elif not same_context:
                    resources["attempted_tools"] = []
                self.prune_resources(state, resources)
                if selected:
                    resources.update({"stock": stock, "policy": policy(selected.value)})
                    resources["context_key"] = [state.code, state.exchange, selected.value]
                # 갱신한 상태와 조사 항목을 모델에 알려 다음 질문 또는 답변의 근거로 쓴다.
                return {"context": asdict(state), "focus": list(RESEARCH_FOCUS[selected]) if selected else [],
                        "research": self.scoped_resources(state, resources),
                        "required_tools": self.required_tools(state),
                        "missing_tools": self.missing_tools(state, resources),
                        "available_data": "required_tools는 필요한 자료 종류이며 전부 다시 호출하라는 뜻이 아닙니다. reusable_tools는 재사용 후보이고, missing_tools는 아직 확보하지 못한 자료입니다."}
            return {"error": "지원하지 않는 도구입니다."}
        except (ValueError, TypeError):
            # 잘못된 도구 입력은 모델이 수정할 수 있도록 도구 결과로 반환한다.
            return {"error": "도구 입력 형식을 확인하고 다시 호출하세요."}

    @staticmethod
    def required_tools(state):
        """기존 조사 기준은 유지하며, 조회 실행 대신 누락 여부만 검사한다."""
        if not state.code or not state.horizon:
            return []
        return ["get_price_analysis", "get_financials", "get_news"] + (
            ["get_events"] if state.horizon == Horizon.SHORT else [])

    def missing_tools(self, state, resources):
        """필수 자료 중 이번 턴에 시도하지 않았고 재사용도 불가능한 종류만 반환한다."""
        available = set(resources.get("attempted_tools", [])) | set(self.reusable_tools(state, resources))
        return [name for name in self.required_tools(state) if name not in available]

    def callable_tools(self, state, expected_context=None):
        """상태 설정이 끝나기 전에는 조회 도구를 제공하지 않는다.

        프롬프트의 순서 지시만으로는 모델이 관점 변경 전에 이전 관점으로 조회할 수 있다.
        이 함수는 전제조건을 강제하되, 확정된 상태에서는 재사용/추가 조회 선택을 허용한다.
        """
        current = [state.code, state.exchange, state.horizon.value if state.horizon else None]
        names = {"search_stock", "set_research_context"}
        if expected_context is None or current == expected_context:
            names.update(self.required_tools(state))
        return [tool for tool in TOOLS if tool["name"] in names]

    def respond(self, message: str) -> str:
        """한 사용자 메시지를 처리하며 필요한 만큼 모델/도구 왕복을 수행한다.

        반환값은 화면용 답변 문자열이다. 부가 결과는 성공 시 self.research,
        self.coverage, self.tool_calls, self.agent_steps에 함께 저장된다.
        API 예외 또는 반복 제한 초과 시 ChatError를 올려 서버가 안전한 오류를 보낸다.
        """
        # 같은 세션의 동시 요청이 이력을 뒤섞지 않도록 한다. 다른 세션은 별도 잠금을 쓴다.
        with self.lock:
            if message.strip() in {"/reset", "초기화"}:
                # 초기화는 모델 판단이 필요 없으므로 API 호출 없이 처리한다.
                self.state = ConversationState()
                self.history = []
                self.tool_calls = []
                self.agent_steps = []
                self.research = {}
                self.saved_resources = {}
                self.coverage = []
                return "새로운 조사를 시작할게요. 어떤 종목이 궁금한가요?"
            key, model = settings()
            if self.client is None and (not key or key == "aaa"):
                raise ChatError("OpenAI API 키가 설정되지 않았어요. 프로젝트 .env의 aaa를 실제 API 키로 변경해 주세요.")
            # 작업용 복사본에서 진행하고 최종 답변에 성공한 경우에만 저장한다.
            # 중간 호출이 실패해도 기존 대화와 조사 상태는 보존된다.
            state = deepcopy(self.state)
            # deepcopy는 중첩 dict/list도 분리한다. 실패한 요청이 원본 상태 일부를 바꾸는
            # 일을 막는 가벼운 트랜잭션 역할이며 DB에 저장하는 것은 아니다.
            # 과거 도구 원문/답변에는 이전 기간 숫자가 남아 있다. 새 요청에는 사용자
            # 발화만 유지하고, 종목·관점과 유효한 자료는 별도 instructions로 재구성한다.
            history = deepcopy([item for item in self.history if item.get("role") == "user"])
            history.append({"role": "user", "content": message})
            # 명확한 입력은 로컬 목록과 대조해 최종 답변의 문맥 누락을 검사한다.
            # 복수 후보와 복수 관점은 여기서 임의로 확정하지 않는다.
            matches = find_stocks(message)
            # 서버의 로컬 사전 확인이다. 모델의 search_stock 요청이 아니므로 로그 횟수에
            # 표시되지 않는다. 일반 대화나 모호한 입력 전체를 분류하는 의도 인식기는 아니다.
            requested_horizon = detect_horizon(message)
            expected_context = None
            if len(matches) == 1:
                stock = matches[0]
                selected = requested_horizon or state.horizon
                expected_context = [stock["code"], stock["exchange"], selected.value if selected else None]
            elif not matches and state.code and requested_horizon:
                expected_context = [state.code, state.exchange, requested_horizon.value]
            require_context = False
            # 역할 지시문에 공통 조사 기준과 이번 대화의 현재 상태를 덧붙인다.
            instructions = PROMPT.read_text(encoding="utf-8")
            instructions += "\n조사 기준:\n" + json.dumps(RESEARCH_FOCUS, ensure_ascii=False)
            trace = []
            steps = []
            # trace/steps는 이번 턴만 기록한다. 실패 시에는 이전 성공 결과를 덮어쓰지 않는다.
            resources = deepcopy(self.saved_resources or self.research)
            resources["attempted_tools"] = []
            # 이전 자료를 명시적으로 다시 제공한다. 모델 자체의 영구 기억에 의존하지 않는다.
            self.prune_resources(state, resources)
            client = self.client or OpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=45, max_retries=0)
            try:
                # 사용자 질문 한 번에 모델을 여러 번 호출할 수 있다. 무한 반복은 제한한다.
                for _ in range(12):
                    context_key = [state.code, state.exchange, state.horizon.value if state.horizon else None]
                    context_missing = expected_context is not None and context_key != expected_context
                    choice = {}
                    if context_missing:
                        choice = ({"tool_choice": {"type": "function", "name": "set_research_context"}}
                                  if require_context else {})
                    view_state = deepcopy(state)
                    # 사용자가 '중기'로 바꾼 첫 호출부터 장기 재무 전체가 노출되지 않도록
                    # 명확히 감지한 목표 관점으로 공개용 자료를 만든다. 실제 상태 확정은
                    # 여전히 모델이 set_research_context를 요청해야 완료된다.
                    if expected_context and context_missing:
                        view_state.code, view_state.exchange = expected_context[:2]
                        view_state.horizon = Horizon(expected_context[2]) if expected_context[2] else None
                    # 변경 후 관점에서도 유효한 자료만 공개한다. 이전 관점의 reusable_tools
                    # 목록이나 뉴스가 남아 모델에게 잘못된 재사용 신호를 주지 않게 한다.
                    view_resources = deepcopy(resources)
                    if context_missing:
                        view_resources["attempted_tools"] = []
                    self.prune_resources(view_state, view_resources)
                    view = self.scoped_resources(view_state, view_resources)
                    if [view_state.code, view_state.exchange] != [state.code, state.exchange]:
                        view = {}
                    bank = evidence_bank(list(RESEARCH_FOCUS[view_state.horizon]) if view_state.horizon else [], view)
                    # 자료마다 이번 호출에서 유효한 ID를 붙인다. 최종 sections의 source_ids가
                    # 어떤 원본을 가리키는지 서버가 확인할 수 있게 이 목록을 모델에도 제공한다.
                    response_instructions = instructions + "\n" + ANSWER_INSTRUCTIONS
                    response_instructions += "\n현재 조사 상태:\n" + json.dumps(asdict(state), ensure_ascii=False)
                    response_instructions += "\n이번 요청의 진행 조건:\n" + json.dumps({
                        "context_update_required": context_missing,
                        "target_context": expected_context,
                        "missing_tools": self.missing_tools(view_state, view_resources),
                        "rule": "context_update_required이면 먼저 상태를 설정하고 결과를 기다립니다. 이후 보유 자료가 충분하면 재사용하고 필요한 조회만 선택합니다."
                    }, ensure_ascii=False)
                    response_instructions += "\n현재 유효한 보유 자료(재사용 여부를 판단하세요):\n" + json.dumps(view, ensure_ascii=False)
                    response_instructions += "\n이번 호출의 근거 목록:\n" + json.dumps(bank, ensure_ascii=False)
                    # 매 호출에 도구 정의와 누적 이력을 전달한다. store=False이므로
                    # 이 앱은 서버 메모리의 history를 이용해 다음 요청의 문맥을 구성한다.
                    offered_tools = self.callable_tools(state, expected_context)
                    offered_names = {tool["name"] for tool in offered_tools}
                    steps.append({"round": _ + 1, "event": "모델 호출", "detail":
                        "질문·현재 상태·도구 설명·현재 범위의 자료를 전달했습니다.",
                        "available_tools": self.reusable_tools(view_state, view_resources),
                        "callable_tools": sorted(offered_names)})
                    response = client.responses.create(
                        model=model, instructions=response_instructions, input=history, tools=offered_tools,
                        store=False, max_output_tokens=4000,
                        text={"format": {"type": "json_schema", "name": "research_answer", "strict": True, "schema": answer_schema(bank)}},
                        **choice,
                    )
                    if response.status != "completed":
                        raise ChatError("모델 답변이 완료되지 않았어요. 질문을 짧게 하거나 다시 시도해 주세요.")
                    # 모델의 도구 요청도 이력에 남겨 후속 실행 결과와 연결한다.
                    history.extend(item.model_dump(exclude_none=True) for item in response.output)
                    calls = [item for item in response.output if item.type == "function_call"]
                    # 한 응답에 여러 도구 요청이 올 수 있다. 현재 서버는 순서대로 실행한다.
                    # tools를 매 호출에 보내는 것과 도구를 실제 실행하는 것은 별개다.
                    # function_call이 있으면 아직 최종 답변이 아니다. Python이 실행한 결과를
                    # call_id로 대응시켜 다음 모델 호출의 입력에 넣어야 한 사이클이 완성된다.
                    if not calls:
                        if context_missing:
                            # 명확한 종목/관점을 무시한 초안은 화면에 내보내지 않는다.
                            # 다음 호출에만 set_research_context를 강제해 정상 검색 흐름은 허용한다.
                            require_context = True
                            steps.append({"round": _ + 1, "event": "서버 보완 요청",
                                          "detail": "종목·투자 관점이 일치하지 않아 초안을 보류하고 맥락 설정을 요청했습니다."})
                            history.append({"role": "developer", "content":
                                "입력에서 확인한 종목 코드·거래소·투자 관점은 "
                                + json.dumps(expected_context, ensure_ascii=False)
                                + " 입니다. set_research_context로 이 문맥을 저장한 후 조사하세요. 지금 작성한 답변은 자료 확인 전 초안이며 사용자에게 전달되지 않았습니다."})
                            continue
                        missing = self.missing_tools(state, resources)
                        # '이번에 호출했는가' 또는 '유효한 보유 자료가 있는가'를 검사한다.
                        # 이 검사로 모델이 자료를 실제 추론에 사용했다는 사실은 알 수 없다.
                        if missing:
                            steps.append({"round": _ + 1, "event": "서버 보완 요청",
                                          "detail": "필수 자료가 부족해 초안을 보류했습니다: " + ", ".join(missing)})
                            # 서버는 대신 조회하지 않는다. 모델에게 누락된 실행을 요청한다.
                            history.append({"role": "developer", "content":
                                "이번 턴의 필수 조회가 누락되었습니다: " + ", ".join(missing)
                                + ". 해당 도구를 호출한 뒤 답변하세요. 조회 실패는 자료 부족으로 명시하세요. 초안은 사용자에게 전달되지 않았습니다."})
                            continue
                        # 도구 실행 요청이 없으면 사용자에게 보여줄 최종 텍스트로 처리한다.
                        try:
                            payload = json.loads(response.output_text)
                            # 모델 출력은 JSON이고 사용자 화면은 일반 문자열이다.
                            # 문단(text)과 여러 근거 ID를 같은 객체로 받아, 결론 문장을 별도로
                            # 다시 인용할 때 생길 수 있는 불일치를 줄인다.
                            sections = payload["sections"]
                            if not isinstance(sections, list) or not sections or any(
                                not isinstance(section, dict) or not isinstance(section.get("text"), str)
                                or not section["text"].strip() or not isinstance(section.get("source_ids"), list)
                                for section in sections
                            ):
                                raise ValueError
                            answer = "\n\n".join(section["text"] for section in sections)
                            claims = [section for section in sections if section["source_ids"]]
                            # 인사·질문 같은 문단은 ID가 없어도 정상이다. 사용 명시만 claims로
                            # 모으며, ID 미명시를 실제 내부 추론의 미사용으로 단정하지 않는다.
                        except (ValueError, AttributeError, KeyError, TypeError):
                            raise ChatError("답변 형식을 확인하지 못했어요. 다시 시도해 주세요.")
                        if not isinstance(answer, str) or not answer.strip():
                            raise ChatError("모델이 답변을 반환하지 않았어요. 다시 시도해 주세요.")
                        self.state, self.history, self.tool_calls = state, history, trace
                        steps.append({"round": _ + 1, "event": "최종 답변", "detail":
                            "추가 도구 요청 없이 답변을 반환했습니다. 응답 형식과 필수 자료 조건을 확인했습니다."})
                        self.agent_steps = steps
                        resources["reused_tools"] = [name for name in self.reusable_tools(state, resources)
                                                     if name not in resources.get("attempted_tools", [])]
                        self.saved_resources = resources
                        # 저장소와 공개 자료를 분리하여 내부 원본이 HTTP 응답에 섞이지 않게 한다.
                        self.research = self.scoped_resources(state, resources)
                        self.coverage = declared_coverage(answer, claims, bank)
                        # ID와 문장 존재를 검증한다. 해당 자료가 결론을 논리적으로 충분히
                        # 뒷받침하는지까지 증명하는 검증은 아니라는 한계가 있다.
                        return answer
                    for call in calls:
                        if len(trace) >= 12:
                            raise ChatError("도구 호출 한도에 도달했어요. 질문 범위를 줄여 주세요.")
                        started = perf_counter()
                        steps.append({"round": _ + 1, "event": "모델 → 파이썬", "detail":
                                      call.name + " 실행 요청", "call_id": call.call_id})
                        # 도구 노출 제한과 실행 검증을 둘 다 적용한다. 같은 응답 안에서
                        # 관점이 바뀌더라도, 제공하지 않았던 도구를 곧바로 실행하지 않는다.
                        allowed_now = {tool["name"] for tool in self.callable_tools(state, expected_context)}
                        if call.name not in offered_names or call.name not in allowed_now:
                            result = {"error": "현재 단계에서 사용할 수 없는 도구입니다. 종목·관점 설정 결과를 확인한 뒤 제공된 도구만 요청하세요."}
                        else:
                            result = self.execute_tool(call.name, call.arguments, state, resources)
                        if call.name == "get_financials" and "error" not in result:
                            result = self.scoped_resources(state, resources)["financials"]
                        if call.name == "set_research_context" and "error" not in result:
                            # 같은 턴 안에서 관점이 바뀐 경우에도 이전 도구 결과/초안을 제거한다.
                            # 현재 호출의 function_call은 보존해 output의 call_id 짝을 유지한다.
                            current_ids = {item.call_id for item in calls}
                            # API 입력에는 각 function_call과 대응 output이 짝으로 남아야 한다.
                            # 같은 응답에서 온 다른 호출도 보존하므로 여러 도구 요청을 처리할 수 있다.
                            history = [item for item in history if item.get("role") == "user"
                                       or item.get("call_id") in current_ids]
                        try:
                            arguments = json.loads(call.arguments)
                        except (ValueError, TypeError):
                            arguments = {"invalid_json": True}
                        # 호출 시점의 결과를 복사해 이후 상태 변경과 실행 내역을 분리한다.
                        trace.append({"name": call.name, "call_id": call.call_id, "round": _ + 1,
                                      "arguments": arguments,
                                      "duration_ms": round((perf_counter() - started) * 1000),
                                      "result": deepcopy(result)})
                        # call_id로 어떤 요청의 결과인지 연결한다. 다음 반복에서 모델이 읽는다.
                        history.append({"type": "function_call_output", "call_id": call.call_id,
                                        "output": json.dumps(result, ensure_ascii=False)})
                        steps.append({"round": _ + 1, "event": "파이썬 실행 완료", "detail":
                                      call.name + (" 오류 결과" if "error" in result else " 결과")
                                      + "를 다음 모델 호출의 입력에 추가했습니다.", "call_id": call.call_id})
                raise ChatError("조사가 길어져 중단했어요. 종목 하나로 다시 질문해 주세요.")
            except (APIConnectionError, APITimeoutError) as error:
                raise ChatError("OpenAI에 연결하지 못했어요. 네트워크를 확인하고 다시 시도해 주세요.") from error
            except APIStatusError as error:
                # 원문에는 키 일부가 포함될 수 있으므로 알려진 오류 코드와 요청 ID만 기록한다.
                body = error.body if isinstance(error.body, dict) else {}
                detail = body.get("error", body)
                code = detail.get("code") if isinstance(detail, dict) else None
                known = {"invalid_api_key", "invalid_organization", "organization_deactivated",
                         "account_deactivated", "ip_not_authorized", "insufficient_quota",
                         "rate_limit_exceeded", "model_not_found", "permission_denied"}
                code = code if isinstance(code, str) and code in known else "unknown"
                request_id = getattr(error, "request_id", None)
                request_id = request_id if isinstance(request_id, str) and re.fullmatch(r"req_[a-zA-Z0-9_-]{1,100}", request_id) else "unavailable"
                logging.getLogger(__name__).warning("OpenAI failure status=%s code=%s request_id=%s", error.status_code, code, request_id)
                messages = {401: "OpenAI가 서버의 인증을 거절했습니다. Render는 Environment, 로컬은 .env 설정이 적용됩니다.",
                            429: "OpenAI 사용량 또는 요청 한도에 도달했어요. API 결제·한도를 확인해 주세요.",
                            403: "이 API 키로 요청한 모델에 접근할 수 없어요.",
                            404: "모델을 찾을 수 없어요. OPENAI_MODEL 설정을 확인해 주세요."}
                message = messages.get(error.status_code, "OpenAI 요청에 실패했어요. 모델 설정을 확인하고 다시 시도해 주세요.")
                raise ChatError(f"{message} [HTTP {error.status_code}, code={code}, request_id={request_id}]") from error
            finally:
                # 여기서 생성한 연결만 닫는다. 외부에서 주입한 테스트 클라이언트는 호출자가 관리한다.
                if self.client is None:
                    client.close()

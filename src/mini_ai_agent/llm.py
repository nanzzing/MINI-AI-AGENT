"""모델 대화의 중심: 사용자 입력 → 모델의 도구 요청 → 실행 결과 전달 → 답변.

server.py와 main.py가 이 모듈을 사용한다. 모델은 실행할 도구를 선택하고,
실제 종목 검색과 상태 변경은 아래 execute_tool에서 파이썬이 수행한다.
"""

from copy import deepcopy
from dataclasses import asdict
import json
import logging
import os
import re
from pathlib import Path
from threading import Lock

from dotenv import dotenv_values
from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError

from mini_ai_agent.conversation import ConversationState, Horizon, RESEARCH_FOCUS
from mini_ai_agent.stocks import find_stocks, load_catalog
from mini_ai_agent.market_data import research_bundle, financials, financial_summary, stock_identity

ROOT = Path(__file__).resolve().parents[2]
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
        self.research: dict = {}
        # 테스트에서는 가짜 응답을 주는 클라이언트를 주입해 실제 API 비용을 피한다.
        self.client = client
        self.lock = Lock()

    def execute_tool(self, name: str, arguments: str, state: ConversationState, resources=None) -> dict:
        """모델이 보낸 JSON 인자를 검사하고 허용한 도구만 실행한다."""
        resources = resources if resources is not None else {}
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError
            if name == "get_financials":
                if not state.code or not state.horizon:
                    return {"error": "먼저 종목과 투자 기간을 set_research_context로 확정하세요."}
                result = financials(stock_identity(state.code, state.exchange), state.horizon.value)
                resources["financials"] = financial_summary(result)
                return resources["financials"]
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
                state.company, state.code, state.exchange = stock["name"], stock["code"], stock["exchange"]
                state.horizon = selected
                state.stage = "ready_for_research" if selected else "awaiting_horizon"
                # 뉴스는 모델의 선택에 맡기지 않는 필수 조사 단계다. 일정은 단기만 포함한다.
                resources.clear()
                if selected:
                    resources.update(research_bundle(stock, selected.value))
                    resources["context_key"] = [state.code, state.exchange, selected.value]
                # 갱신한 상태와 조사 항목을 모델에 알려 다음 질문 또는 답변의 근거로 쓴다.
                return {"context": asdict(state), "focus": list(RESEARCH_FOCUS[selected]) if selected else [],
                        "research": resources,
                        "available_data": "가격·뉴스·단기 실적 일정. 재무가 필요하면 get_financials를 호출하세요."}
            return {"error": "지원하지 않는 도구입니다."}
        except (ValueError, TypeError):
            # 잘못된 도구 입력은 모델이 수정할 수 있도록 도구 결과로 반환한다.
            return {"error": "도구 입력 형식을 확인하고 다시 호출하세요."}

    def respond(self, message: str) -> str:
        # 같은 세션의 동시 요청이 이력을 뒤섞지 않도록 한다. 다른 세션은 별도 잠금을 쓴다.
        with self.lock:
            if message.strip() in {"/reset", "초기화"}:
                # 초기화는 모델 판단이 필요 없으므로 API 호출 없이 처리한다.
                self.state = ConversationState()
                self.history = []
                self.tool_calls = []
                self.research = {}
                return "새로운 조사를 시작할게요. 어떤 종목이 궁금한가요?"
            key, model = settings()
            if self.client is None and (not key or key == "aaa"):
                raise ChatError("OpenAI API 키가 설정되지 않았어요. 프로젝트 .env의 aaa를 실제 API 키로 변경해 주세요.")
            # 작업용 복사본에서 진행하고 최종 답변에 성공한 경우에만 저장한다.
            # 중간 호출이 실패해도 기존 대화와 조사 상태는 보존된다.
            state = deepcopy(self.state)
            history = deepcopy(self.history)
            history.append({"role": "user", "content": message})
            # 역할 지시문에 공통 조사 기준과 이번 대화의 현재 상태를 덧붙인다.
            instructions = PROMPT.read_text(encoding="utf-8")
            instructions += "\n조사 기준:\n" + json.dumps(RESEARCH_FOCUS, ensure_ascii=False)
            instructions += "\n현재 조사 상태:\n" + json.dumps(asdict(state), ensure_ascii=False)
            trace = []
            resources = {}
            client = self.client or OpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=45, max_retries=0)
            try:
                # 사용자 질문 한 번에 모델을 여러 번 호출할 수 있다. 무한 반복은 제한한다.
                for _ in range(6):
                    # 매 호출에 도구 정의와 누적 이력을 전달한다. store=False이므로
                    # 이 앱은 서버 메모리의 history를 이용해 다음 요청의 문맥을 구성한다.
                    response = client.responses.create(
                        model=model, instructions=instructions, input=history, tools=TOOLS,
                        store=False, max_output_tokens=1800,
                    )
                    if response.status != "completed":
                        raise ChatError("모델 답변이 완료되지 않았어요. 질문을 짧게 하거나 다시 시도해 주세요.")
                    # 모델의 도구 요청도 이력에 남겨 후속 실행 결과와 연결한다.
                    history.extend(item.model_dump(exclude_none=True) for item in response.output)
                    calls = [item for item in response.output if item.type == "function_call"]
                    if not calls:
                        # 후속 분석에서 상태 저장 도구를 생략해도 이번 턴의 필수 자료를 확보한다.
                        context_key = [state.code, state.exchange, state.horizon.value if state.horizon else None]
                        if state.code and state.horizon and resources.get("context_key") != context_key:
                            resources.update(research_bundle(stock_identity(state.code, state.exchange), state.horizon.value))
                            resources["context_key"] = context_key
                            history.append({"role": "developer", "content": "이번 요청의 필수 조사 자료입니다. 자료의 status와 시점을 확인하여 답변하세요.\n" + json.dumps(resources, ensure_ascii=False)})
                            continue
                        # 도구 실행 요청이 없으면 사용자에게 보여줄 최종 텍스트로 처리한다.
                        answer = response.output_text.strip()
                        if not answer:
                            raise ChatError("모델이 답변을 반환하지 않았어요. 다시 시도해 주세요.")
                        self.state, self.history, self.tool_calls = state, history, trace
                        self.research = resources
                        return answer
                    for call in calls:
                        if len(trace) >= 12:
                            raise ChatError("도구 호출 한도에 도달했어요. 질문 범위를 줄여 주세요.")
                        result = self.execute_tool(call.name, call.arguments, state, resources)
                        trace.append({"name": call.name, "result": result})
                        # call_id로 어떤 요청의 결과인지 연결한다. 다음 반복에서 모델이 읽는다.
                        history.append({"type": "function_call_output", "call_id": call.call_id,
                                        "output": json.dumps(result, ensure_ascii=False)})
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

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
from itertools import count
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
from mini_ai_agent.stocks import load_catalog, known_stocks, search_stocks
from mini_ai_agent.market_data import financials, financial_summary, stock_identity, price_history, news, earnings_events, policy
from mini_ai_agent.technicals import analyze
from mini_ai_agent.coverage import evidence_bank, declared_coverage, answer_schema, ANSWER_INSTRUCTIONS
from mini_ai_agent.chatgpt import ChatGPTError
from mini_ai_agent.valuation import valuation_comparison
from mini_ai_agent.market_data import topic_news

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


def api_model_options(default=None):
    """운영자가 허용한 API 모델만 공개한다. 계정별 실제 사용 권한 조회는 아니다."""
    default = default or settings()[1]
    labels = {
        'gpt-4.1-mini': 'GPT-4.1 mini',
        'gpt-5.4-mini': 'GPT-5.4 mini',
        'gpt-5.4': 'GPT-5.4',
        'gpt-6-luna': 'GPT-6 Luna',
        'gpt-6-sol': 'GPT-6 Sol',
        'gpt-6.1-sol': 'GPT-6.1 Sol',
        'gpt-6-astra': 'GPT-6 Astra',
    }
    values = dotenv_values(ROOT / '.env')
    configured = os.environ.get('OPENAI_ALLOWED_MODELS', values.get('OPENAI_ALLOWED_MODELS'))
    candidates = configured.split(',') if configured is not None else [default, *labels]
    models = list(dict.fromkeys(value.strip() for value in candidates
                               if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value.strip())))[:20]
    if not models:
        models = [default]
    selected = default if default in models else models[0]
    return selected, [{'id': model, 'label': labels.get(model, model)} for model in models]


def configuration_status() -> dict:
    # 화면에는 키 자체를 보내지 않는다. configured는 키 유효성 검증 결과가 아니다.
    key, model = settings()
    model, models = api_model_options(model)
    return {"configured": bool(key and key != "aaa"), "model": model, 'api_models': models}


# 모델에게 보내는 도구 설명서다. 함수 구현이나 전체 종목 목록은 여기에 포함하지 않는다.
# strict와 JSON Schema로 입력 형태를 지정하고, 실행 시에도 별도로 검증한다.
TOOLS = [
    {"type": "function", "name": "search_topic_news",
     "description": "업황·수요·산업 촉매의 최근 뉴스 제목 검색. 종목과 투자 기간 설정 불필요. 본문/가이던스 원문은 제공하지 않습니다.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "days": {"type": "integer", "enum": [7, 30, 90]}}, "required": ["query", "days"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "get_company_data",
     "description": "검색으로 검증한 기업의 필요한 자료만 조회. 현재 조사 종목을 변경하지 않아 여러 기업 비교 가능. horizon은 자료 범위이며 사용자의 투자 기간으로 저장하지 않음. valuation은 장기 범위만 허용.",
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string"}, "exchange": {"type": "string"},
         "horizon": {"type": "string", "enum": ["단기", "중기", "장기"]},
         "kind": {"type": "string", "enum": ["financials", "news", "technicals", "valuation"]}},
         "required": ["code", "exchange", "horizon", "kind"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "set_response_mode",
     "description": "질문 의도 선택. research=개별 종목 전체 분석/관점 전환, industry=업황·수요 전망, compare=여러 기업 비교(업황 병행 가능), explain=직전 답변 설명, concept=개념, focused=특정 항목. 일반 질문은 반드시 적합한 모드를 선택해 기존 체크리스트와 분리합니다.",
     "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["research", "explain", "concept", "focused", "industry", "compare"]}},
                    "required": ["mode"], "additionalProperties": False}, "strict": True},
    *[{"type": "function", "name": name, "description": description,
       "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True}
      for name, description in [
          ("get_price_analysis", "현재 종목·관점의 가격 이력 요약과 파이썬이 계산한 기술적 지표를 조회합니다. RSI·MACD와 각각의 상승·하락 다이버전스 판정 및 비교 전환점을 포함합니다."),
          ("get_news", "현재 종목·관점에 맞는 최근 뉴스 제목과 출처를 조회합니다. 모든 관점에서 필수입니다."),
          ("get_events", "단기 관점의 향후 실적 발표 예상 일정을 조회합니다. 중기·장기에는 호출하지 마세요."),
          ("get_valuation_comparison", "장기 전용 가치평가. 한국은 DART 최근 4분기 실적과 최근 종가로 기업 전체 TTM PER을 계산하며, PBR은 최신 분기 말 자본, ROE는 TTM 이익과 기간 시작·끝 평균 자본 기준입니다. 미국은 공급자 배수입니다. 선행/업종/과거 등 미제공 자료는 비교 불가로 설명하세요."),
      ]],
    {"type": "function", "name": "get_financials",
     "description": "현재 조사 종목의 재무제표를 투자 기간별 범위로 조회합니다. 실적·성장·부채·가치 분석 시 사용하세요.",
     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "search_stock",
     "description": "종목명·별칭·티커로 로컬 목록을 검색하고 없으면 외부에서 한국·미국 주식 후보를 조회합니다. 한글명 실패 시 영문명/정확한 티커로 재검색하세요. A/B·우선주를 구분하며 주가/재무는 반환하지 않습니다.",
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

    def __init__(self, client=None, chatgpt=None):
        self.state = ConversationState()
        self.history: list[dict] = []
        self.tool_calls: list[dict] = []
        self.agent_steps: list[dict] = []
        self.research: dict = {}
        self.saved_resources: dict = {}  # 원본 보관용. API 응답이나 모델 입력에는 직접 넣지 않는다.
        self.coverage: list[dict] = []
        self.dialogue: list[dict] = []  # 최근 성공한 4턴. 과거 답변은 현재 시장 자료와 분리한다.
        # state: 종목/관점, history: 모델에게 재전송할 메시지와 도구 결과,
        # tool_calls: 이번 답변의 실제 모델 요청 내역, research: 이번 조회 자료,
        # coverage: 같은 모델 응답에서 선언한 자료 사용 관계. 서로 다른 목적의 저장소다.
        # 테스트에서는 가짜 응답을 주는 클라이언트를 주입해 실제 API 비용을 피한다.
        self.client = client
        self.api_model = None  # 이 대화의 API 모델. 다른 사용자나 환경변수는 변경하지 않는다.
        self.chatgpt = chatgpt  # 로컬 구독 연결. None이면 기존 API 키 경로다.
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
        if 'question_sources' in view:
            now = datetime.now(timezone.utc).timestamp()
            view['question_sources'] = {key: item for key, item in view['question_sources'].items()
                                        if now - item['timestamp'] < 300}
        if state.horizon != Horizon.LONG:
            view.pop('valuation', None)
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
            if name == "get_valuation_comparison" and state.horizon != Horizon.LONG:
                continue
            eligible.append(name)
        return eligible

    def prune_resources(self, state, resources):
        """유효하지 않은 자료는 현재 근거 목록에서도 제거한다."""
        # 이번 턴에 시도했지만 실패한 자료는 남겨 모델에게 '실패했다'는 사실을 전달한다.
        # 다음 턴에는 attempted_tools를 비우므로 실패 결과를 유효한 캐시로 승격하지 않는다.
        allowed = set(self.reusable_tools(state, resources)) | set(resources.get("attempted_tools", []))
        fields = {"get_price_analysis": ["prices", "technicals"], "get_financials": ["financials", "financials_context_key"],
                  "get_news": ["news"], "get_events": ["events"], "get_valuation_comparison": ["valuation"]}
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
            if name in {'search_topic_news', 'get_company_data'}:
                # 자동 조사는 현재 단일 종목 상태와 분리한다. 키에 종목/자료/기간을 모두 포함한다.
                if name == 'search_topic_news':
                    if set(args) != {'query', 'days'} or not isinstance(args['query'], str) or not 1 <= len(args['query'].strip()) <= 200 or args['days'] not in (7, 30, 90):
                        raise ValueError
                    label = args['query'].strip()
                else:
                    if set(args) != {'code', 'exchange', 'horizon', 'kind'} or args['horizon'] not in ('단기', '중기', '장기') or args['kind'] not in ('financials', 'news', 'technicals', 'valuation'):
                        raise ValueError
                    identity = (args['code'], args['exchange'])
                    if identity != (state.code, state.exchange) and not any((s['code'], s['exchange']) == identity for s in resources.get('_stock_candidates', [])):
                        return {'error': '먼저 search_stock으로 이 기업을 확인하세요.'}
                    stock = stock_identity(*identity)
                    if args['kind'] == 'valuation' and args['horizon'] != '장기':
                        return {'error': '가치평가 자료 범위는 장기만 허용합니다.'}
                    label = f"{stock['name']} · {args['horizon']} · {args['kind']}"
                key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                store = resources.setdefault('question_sources', {})
                now = datetime.now(timezone.utc).timestamp()
                previous = store.get(key)
                if previous and now - previous['timestamp'] < 300:
                    return {**previous, 'reused': True}
                if name == 'search_topic_news':
                    result = topic_news(args['query'], args['days'])
                elif args['kind'] == 'financials':
                    result = financials(stock, args['horizon'])
                elif args['kind'] == 'news':
                    result = news(stock, args['horizon'])
                elif args['kind'] == 'valuation':
                    result = valuation_comparison(stock)
                else:
                    result = analyze(price_history(stock, args['horizon']))
                entry = {'label': label, 'request': args, 'timestamp': now, 'data': result}
                store[key] = entry
                while len(store) > 12:
                    del store[next(iter(store))]
                return entry
            if name == "set_response_mode":
                mode = args.get("mode")
                if set(args) != {"mode"} or mode not in {"research", "explain", "concept", "focused", "industry", "compare"}:
                    raise ValueError
                resources["_response_mode"] = mode
                return {"mode": mode, "rule": "과거 답변 설명은 당시 기준으로만 합니다. 새 종목/관점 분석은 research이며, 최신 사실은 유효한 자료 또는 새 조회가 필요합니다."}
            if name in {"get_financials", "get_price_analysis", "get_news", "get_events", "get_valuation_comparison"}:
                if args:
                    raise ValueError
                if not state.code or not state.horizon:
                    return {"error": "먼저 종목과 투자 기간을 set_research_context로 확정하세요."}
                stock = stock_identity(state.code, state.exchange)
                if name == "get_events" and state.horizon != Horizon.SHORT:
                    return {"error": "일정은 단기 관점에서만 조회합니다."}
                if name == 'get_valuation_comparison' and state.horizon != Horizon.LONG:
                    return {'error': '가치평가 비교는 장기 관점에서만 조회합니다.'}
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
                elif name == 'get_valuation_comparison':
                    result = resources['valuation'] = valuation_comparison(stock)
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
                # 로컬 우선·외부 보완 검색. 모델에는 전체 목록이 아닌 후보만 보낸다.
                result = search_stocks(query)
                # 이 대화에서 실제 검색으로 반환된 후보만 새 조사 대상으로 허용한다.
                # 다른 사용자의 검색 캐시에 존재한다는 이유만으로 검증을 생략하지 않는다.
                candidates = resources.setdefault('_stock_candidates', [])
                for stock in result.get('stocks', []):
                    if stock not in candidates:
                        candidates.append(stock)
                resources['_stock_candidates'] = candidates[-40:]
                return result
            if name == "set_research_context":
                # 모델이 만든 코드·거래소를 그대로 믿지 않고 실제 목록과 대조한다.
                matches = [row for row in known_stocks()
                           if row["code"] == args.get("code") and row["exchange"] == args.get("exchange")]
                if len(matches) != 1:
                    return {"error": "해당 종목을 확인할 수 없습니다. search_stock으로 다시 검색하세요."}
                identity = (args.get('code'), args.get('exchange'))
                verified = any((row['code'], row['exchange']) == identity
                               for row in resources.get('_stock_candidates', []))
                if identity != (state.code, state.exchange) and not verified:
                    return {'error': '새 종목은 먼저 search_stock으로 확인해야 합니다. 해석한 정식 회사명 또는 티커를 검색하고 반환된 후보로 다시 설정하세요.'}
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
                    candidates = resources.get('_stock_candidates', [])
                    question_sources = resources.get('question_sources', {})
                    mode = resources.get('_response_mode', 'research')
                    resources.clear()
                    resources['_stock_candidates'] = candidates
                    resources['question_sources'] = question_sources
                    resources['_response_mode'] = mode
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
            ["get_events"] if state.horizon == Horizon.SHORT else
            ["get_valuation_comparison"] if state.horizon == Horizon.LONG else [])

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
        names = {"search_stock", "set_research_context", "set_response_mode", "search_topic_news", "get_company_data"}
        if expected_context is None or current == expected_context:
            names.update(self.required_tools(state))
        return [tool for tool in TOOLS if tool["name"] in names]

    def respond(self, message: str, on_progress=None, *, selection="자동", api_model=None) -> str:
        """한 사용자 메시지를 처리하며 필요한 만큼 모델/도구 왕복을 수행한다.

        반환값은 화면용 답변 문자열이다. 부가 결과는 성공 시 self.research,
        self.coverage, self.tool_calls, self.agent_steps에 함께 저장된다.
        최종 답변을 얻을 때까지 횟수 제한 없이 왕복한다. API/통신 오류는
        ChatError로 전달하며, 조회 권한·필수 자료·답변 형식 검증은 그대로 유지한다.
        on_progress는 서버가 관찰한 실행 단계만 전달한다. 모델의 내부 사고가 아니다.
        """
        turn_started = perf_counter()
        steps = []

        def record_step(step):
            # 작업 중 이력과 성공한 대화 상태는 분리한다. callback은 원본을 변경할 수 없다.
            step = {**step, "elapsed_ms": round((perf_counter() - turn_started) * 1000)}
            steps.append(step)
            if on_progress is not None:
                on_progress(deepcopy(step))

        record_step({"round": 0, "event": "대화 처리 대기", "detail": "이전 요청이 있으면 완료를 기다립니다."})
        # 같은 세션의 동시 요청이 이력을 뒤섞지 않도록 한다. 다른 세션은 별도 잠금을 쓴다.
        with self.lock:
            if api_model is not None:
                if self.chatgpt is not None or not isinstance(api_model, str) or api_model not in {row['id'] for row in api_model_options()[1]}:
                    raise ChatError('허용되지 않은 API 모델입니다. 화면에서 모델을 다시 선택해 주세요.')
                self.api_model = api_model
            record_step({"round": 0, "event": "연결·설정 확인", "detail": "대화 상태와 모델 연결 설정을 확인합니다."})
            if message.strip() in {"/reset", "초기화"}:
                # 초기화는 모델 판단이 필요 없으므로 API 호출 없이 처리한다.
                self.state = ConversationState()
                self.history = []
                self.dialogue = []
                self.tool_calls = []
                self.agent_steps = []
                self.research = {}
                self.saved_resources = {}
                self.coverage = []
                return "새로운 조사를 시작할게요. 어떤 종목이 궁금한가요?"
            if self.chatgpt is not None and self.client is None:
                try:
                    client, model = self.chatgpt.client()
                except ChatGPTError as error:
                    raise ChatError(str(error)) from error
                except (OSError, ValueError, TimeoutError) as error:
                    raise ChatError("로컬 ChatGPT 인증 저장소를 열지 못했어요.") from error
            else:
                key, model = settings()
                default, options = api_model_options(model)
                model = self.api_model if self.api_model in {row['id'] for row in options} else default
                self.api_model = model
                if self.client is None and (not key or key == "aaa"):
                    raise ChatError("OpenAI API 키가 설정되지 않았어요. 프로젝트 .env의 aaa를 실제 API 키로 변경해 주세요.")
                client = self.client or OpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=45, max_retries=0)
            # 작업용 복사본에서 진행하고 최종 답변에 성공한 경우에만 저장한다.
            # 중간 호출이 실패해도 기존 대화와 조사 상태는 보존된다.
            state = deepcopy(self.state)
            # deepcopy는 중첩 dict/list도 분리한다. 실패한 요청이 원본 상태 일부를 바꾸는
            # 일을 막는 가벼운 트랜잭션 역할이며 DB에 저장하는 것은 아니다.
            # 과거 도구 원문은 재전송하지 않는다. 최근 질문은 input에, 답변과 당시
            # 사용 근거는 별도 대화 기록에 넣어 현재 유효한 자료와 구분한다.
            history = [{"role": "user", "content": turn["question"]} for turn in self.dialogue]
            history.append({"role": "user", "content": message})
            # 종목 해석은 모델에게 맡긴다. 로컬 별칭/문자열 검색 결과를 목표 종목으로
            # 강제하지 않는다. 새 종목은 실제 search_stock 반환 후보여야 설정할 수 있다.
            # 순수 관점 전환만 기존 종목을 유지하며 첫 호출의 자료 범위를 좁힌다.
            requested_horizon = detect_horizon(message)
            expected_context = None
            if state.code and requested_horizon and message.strip().strip(' ?!.') in {
                '단기', '중기', '중장기', '장기', '단기로', '중기로', '중장기로', '장기로',
                '그럼 단기는', '그럼 중기는', '그럼 장기는',
            }:
                expected_context = [state.code, state.exchange, requested_horizon.value]
            require_context = False
            require_search = False
            repair_missing = False
            # 역할 지시문에 공통 조사 기준과 이번 대화의 현재 상태를 덧붙인다.
            instructions = PROMPT.read_text(encoding="utf-8")
            instructions += "\n화면 선택: " + selection + " (사용자가 문장에 명시한 기간이 우선). 자동은 투자 기간이 아니라 질문 의도에 맞춘 처리 방식이다."
            instructions += "\n조사 기준:\n" + json.dumps(RESEARCH_FOCUS, ensure_ascii=False)
            trace = []
            # trace/steps는 이번 턴만 기록한다. 실패 시에는 이전 성공 결과를 덮어쓰지 않는다.
            resources = deepcopy(self.saved_resources or self.research)
            resources["_response_mode"] = "research"
            resources["attempted_tools"] = []
            initial_context = (state.code, state.exchange, state.horizon)

            def turn_missing(current_state, current_resources):
                # 모드 선택은 모델에게 맡기되 새 종목/관점 분석의 필수 자료 검증은 우회하지 않는다.
                changed = (current_state.code, current_state.exchange, current_state.horizon) != initial_context
                mode = current_resources.get("_response_mode", "research")
                if mode in {'industry', 'compare'}:
                    return []
                if not changed and mode in {"concept", "focused"}:
                    return []
                if not changed and mode == "explain" and self.dialogue:
                    return []
                return self.missing_tools(current_state, current_resources)
            # 이전 자료를 명시적으로 다시 제공한다. 모델 자체의 영구 기억에 의존하지 않는다.
            self.prune_resources(state, resources)
            try:
                # count는 횟수를 기록할 뿐 상한을 두지 않는다. 도구 요청이 끝나고
                # 검증을 통과한 최종 답변이 나오면 아래 return에서 반복을 종료한다.
                for round_number in count(1):
                    record_step({"round": round_number, "event": "요청 구성", "detail": "이번 관점의 자료와 근거 목록을 구성합니다."})
                    context_key = [state.code, state.exchange, state.horizon.value if state.horizon else None]
                    context_missing = expected_context is not None and context_key != expected_context
                    choice = {}
                    if require_search:
                        choice = {"tool_choice": {"type": "function", "name": "search_stock"}}
                    elif context_missing:
                        choice = ({"tool_choice": {"type": "function", "name": "set_research_context"}}
                                  if require_context else {})
                    elif repair_missing:
                        missing = turn_missing(state, resources)
                        if missing:
                            # 답변 초안을 다시 요구하지 않고, 아직 시도하지 않은 조회를
                            # function_call로 받는다. 실행은 아래의 검증 경로만 사용한다.
                            choice = {"tool_choice": {"type": "function", "name": missing[0]}}
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
                    general = resources.get('_response_mode') != 'research'
                    checklist = resources.get('_response_mode') in {'research', 'focused'}
                    bank = evidence_bank(list(RESEARCH_FOCUS[view_state.horizon]) if view_state.horizon and checklist else [], view)
                    if general:
                        now = datetime.now(timezone.utc).timestamp()
                        for entry in view.get('question_sources', {}).values():
                            if now - entry['timestamp'] > 300:
                                continue
                            data = entry['data']
                            index = len(bank)
                            sources = [] if data.get('status') not in {'ok', 'partial'} else [{
                                'id': f'q{index}', 'path': 'question_sources', 'value': data,
                                'source': data.get('source', ''), 'as_of': data.get('as_of', ''),
                                'fetched_at': data.get('fetched_at', ''), 'status': data.get('status', '')}]
                            bank.append({'id': index, 'criterion': entry['label'], 'sources': sources,
                                         'limitations': '조회 범위와 출처를 확인하세요. 뉴스는 제목만 제공됩니다.'})
                    # 자료마다 이번 호출에서 유효한 ID를 붙인다. 최종 sections의 source_ids가
                    # 어떤 원본을 가리키는지 서버가 확인할 수 있게 이 목록을 모델에도 제공한다.
                    response_instructions = instructions + "\n" + ANSWER_INSTRUCTIONS
                    response_instructions += "\n현재 조사 상태:\n" + json.dumps(asdict(state), ensure_ascii=False)
                    response_instructions += "\n이 대화에서 검색으로 확인한 종목 후보(사용자 선택과 동일하지 않음):\n" + json.dumps(resources.get('_stock_candidates', []), ensure_ascii=False)
                    response_instructions += "\n과거 대화 기록(당시 답변과 근거이며 최신 자료가 아님; 과거 ID 재사용 금지):\n" + json.dumps(self.dialogue, ensure_ascii=False)
                    response_instructions += "\n이번 응답 모드: " + resources.get("_response_mode", "research")
                    response_instructions += "\n이번 요청의 진행 조건:\n" + json.dumps({
                        "context_update_required": context_missing,
                        "target_context": expected_context,
                        "missing_tools": turn_missing(view_state, view_resources),
                        "rule": "context_update_required이면 먼저 상태를 설정하고 결과를 기다립니다. 이후 보유 자료가 충분하면 재사용하고 필요한 조회만 선택합니다."
                    }, ensure_ascii=False)
                    response_instructions += "\n현재 유효한 보유 자료(재사용 여부를 판단하세요):\n" + json.dumps(view, ensure_ascii=False)
                    response_instructions += "\n이번 호출의 근거 목록:\n" + json.dumps(bank, ensure_ascii=False)
                    # 매 호출에 도구 정의와 누적 이력을 전달한다. store=False이므로
                    # 이 앱은 서버 메모리의 history를 이용해 다음 요청의 문맥을 구성한다.
                    offered_tools = self.callable_tools(state, expected_context)
                    offered_names = {tool["name"] for tool in offered_tools}
                    record_step({"round": round_number, "event": "모델 호출", "detail":
                        "질문·현재 상태·도구 설명·현재 범위의 자료를 전달했습니다.",
                        "available_tools": self.reusable_tools(view_state, view_resources),
                        "callable_tools": sorted(offered_names)})
                    model_started = perf_counter()
                    response = client.responses.create(
                        model=model, instructions=response_instructions, input=history, tools=offered_tools,
                        store=False, max_output_tokens=4000,
                        text={"format": {"type": "json_schema", "name": "research_answer", "strict": True, "schema": answer_schema(bank)}},
                        **choice,
                    )
                    record_step({"round": round_number, "event": "모델 응답 수신",
                                 "detail": "모델 응답을 받아 도구 요청 여부를 확인합니다.",
                                 "duration_ms": round((perf_counter() - model_started) * 1000)})
                    if response.status != "completed":
                        raise ChatError("모델 답변이 완료되지 않았어요. 질문을 짧게 하거나 다시 시도해 주세요.")
                    # 모델의 도구 요청도 이력에 남겨 후속 실행 결과와 연결한다.
                    history.extend(item.model_dump(exclude_none=True) for item in response.output)
                    calls = [item for item in response.output if item.type == "function_call"]
                    if any(call.name == 'search_stock' for call in calls):
                        require_search = False
                    if choice and not calls:
                        raise ChatError("필수 도구 요청을 지정했지만 모델이 도구 호출을 반환하지 않았어요: "
                                        + choice["tool_choice"]["name"]
                                        + ". 모델 연결의 도구 호출 지원을 확인해야 합니다.")
                    # 한 응답에 여러 도구 요청이 올 수 있다. 현재 서버는 순서대로 실행한다.
                    # tools를 매 호출에 보내는 것과 도구를 실제 실행하는 것은 별개다.
                    # function_call이 있으면 아직 최종 답변이 아니다. Python이 실행한 결과를
                    # call_id로 대응시켜 다음 모델 호출의 입력에 넣어야 한 사이클이 완성된다.
                    if not calls:
                        if (not state.code and requested_horizon and resources.get('_response_mode') == 'research'
                                and not any(row['name'] == 'search_stock' for row in trace)):
                            require_search = True
                            record_step({'round': round_number, 'event': '서버 보완 요청',
                                         'detail': '종목 확인 전 분석 초안을 보류하고 검색을 요청했습니다.'})
                            history.append({'role': 'developer', 'content':
                                            '아직 검색으로 확인한 조사 종목이 없습니다. 사용자 표현을 해석하여 search_stock으로 확인하세요. 종목이 불명확하면 확인 질문을 하세요.'})
                            continue
                        if context_missing:
                            # 명확한 종목/관점을 무시한 초안은 화면에 내보내지 않는다.
                            # 다음 호출에만 set_research_context를 강제해 정상 검색 흐름은 허용한다.
                            require_context = True
                            record_step({"round": round_number, "event": "서버 보완 요청",
                                          "detail": "종목·투자 관점이 일치하지 않아 초안을 보류하고 맥락 설정을 요청했습니다."})
                            history.append({"role": "developer", "content":
                                "입력에서 확인한 종목 코드·거래소·투자 관점은 "
                                + json.dumps(expected_context, ensure_ascii=False)
                                + " 입니다. set_research_context로 이 문맥을 저장한 후 조사하세요. 지금 작성한 답변은 자료 확인 전 초안이며 사용자에게 전달되지 않았습니다."})
                            continue
                        missing = turn_missing(state, resources)
                        # '이번에 호출했는가' 또는 '유효한 보유 자료가 있는가'를 검사한다.
                        # 이 검사로 모델이 자료를 실제 추론에 사용했다는 사실은 알 수 없다.
                        if missing:
                            repair_missing = True
                            record_step({"round": round_number, "event": "서버 보완 요청",
                                          "detail": "필수 자료가 부족해 초안을 보류했습니다: " + ", ".join(missing)})
                            # 서버는 대신 조회하지 않는다. 모델에게 누락된 실행을 요청한다.
                            history.append({"role": "developer", "content":
                                "이번 턴의 필수 조회가 누락되었습니다: " + ", ".join(missing)
                                + ". 해당 도구를 호출한 뒤 답변하세요. 조회 실패는 자료 부족으로 명시하세요. 초안은 사용자에게 전달되지 않았습니다."})
                            continue
                        # 도구 실행 요청이 없으면 사용자에게 보여줄 최종 텍스트로 처리한다.
                        record_step({"round": round_number, "event": "답변 검증",
                                     "detail": "최종 답변 형식과 근거 연결을 확인합니다."})
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
                        resources["reused_tools"] = [name for name in self.reusable_tools(state, resources)
                                                     if name not in resources.get("attempted_tools", [])]
                        # 저장소와 공개 자료를 분리하여 내부 원본이 HTTP 응답에 섞이지 않게 한다.
                        research = self.scoped_resources(state, resources)
                        research['response_mode'] = resources.get('_response_mode', 'research')
                        coverage = declared_coverage(answer, claims, bank)
                        record_step({"round": round_number, "event": "최종 답변", "detail":
                            "응답 형식과 필수 자료 조건을 확인하고 최종 답변을 전달합니다."})
                        self.state, self.history, self.tool_calls = state, history, trace
                        self.agent_steps = steps
                        self.saved_resources, self.research, self.coverage = resources, research, coverage
                        used_ids = {source_id for claim in claims for source_id in claim["source_ids"]}
                        self.dialogue = (self.dialogue + [{
                            "question": message, "answer": answer, "context": asdict(state),
                            "answered_at": datetime.now(timezone.utc).isoformat(),
                            "evidence": [source for item in bank for source in item["sources"] if source["id"] in used_ids],
                        }])[-4:]
                        # ID와 문장 존재를 검증한다. 해당 자료가 결론을 논리적으로 충분히
                        # 뒷받침하는지까지 증명하는 검증은 아니라는 한계가 있다.
                        return answer
                    for call in calls:
                        started = perf_counter()
                        record_step({"round": round_number, "event": "모델 → 파이썬", "detail":
                                      call.name + " 실행 요청", "tool": call.name, "call_id": call.call_id})
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
                            # 구독용 추론 모델은 function_call과 함께 암호화된 reasoning
                            # 항목을 반환할 수 있다. 같은 응답의 항목은 다음 왕복에도 유지한다.
                            reasoning_ids = {item.id for item in response.output if item.type == 'reasoning'}
                            # API 입력에는 각 function_call과 대응 output이 짝으로 남아야 한다.
                            # 같은 응답에서 온 다른 호출도 보존하므로 여러 도구 요청을 처리할 수 있다.
                            history = [item for item in history if item.get("role") == "user"
                                       or item.get("call_id") in current_ids or item.get('id') in reasoning_ids]
                        try:
                            arguments = json.loads(call.arguments)
                        except (ValueError, TypeError):
                            arguments = {"invalid_json": True}
                        # 호출 시점의 결과를 복사해 이후 상태 변경과 실행 내역을 분리한다.
                        duration_ms = round((perf_counter() - started) * 1000)
                        trace.append({"name": call.name, "call_id": call.call_id, "round": round_number,
                                      "arguments": arguments,
                                      "duration_ms": duration_ms,
                                      "result": deepcopy(result)})
                        # call_id로 어떤 요청의 결과인지 연결한다. 다음 반복에서 모델이 읽는다.
                        history.append({"type": "function_call_output", "call_id": call.call_id,
                                        "output": json.dumps(result, ensure_ascii=False)})
                        record_step({"round": round_number, "event": "파이썬 실행 완료", "detail":
                                      call.name + (" 오류 결과" if "error" in result else " 결과")
                                      + "를 다음 모델 호출의 입력에 추가했습니다.", "call_id": call.call_id,
                                      "tool": call.name, "duration_ms": duration_ms})
            except ChatGPTError as error:
                raise ChatError(str(error)) from error
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

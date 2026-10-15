"""실제 API 호출 없이 모델 응답·도구 실행·대화 보존·오류 처리를 검증한다."""

import json
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from mini_ai_agent.llm import ChatError, OpenAIConversation


def tool(name, args, identifier):
    """모델이 반환하는 도구 실행 요청 모양의 테스트 데이터를 만든다."""
    return {"type": "function_call", "id": identifier, "call_id": identifier,
            "name": name, "arguments": json.dumps(args), "status": "completed"}


def answer(text, claims=None):
    """도구 호출이 끝난 뒤의 최종 텍스트 응답을 재현한다."""
    return {"type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": json.dumps({"sections": claims or [{"text": text, "source_ids": []}]}, ensure_ascii=False), "annotations": []}]}


def queries(short=False):
    # 서버가 자동 조회하는 흉내가 아니라 모델이 실제 반환할 function_call 배열을 만든다.
    # short=True일 때 일정 도구까지 포함하여 관점별 필수 자료 검사를 통과시킨다.
    names = ["get_price_analysis", "get_financials", "get_news"] + (["get_events"] if short else [])
    return [tool(name, {}, name) for name in names]


class LLMTests(unittest.TestCase):
    def test_scoped_financials_preserve_original_and_recount_missing(self):
        # 5년 원본을 3년/0년으로 잘라도 원본이 남아 있어 다시 5년을 제공할 수 있어야 한다.
        # partial → ok 변화는 '좁혀진 요청 범위 충족'이지 새 데이터를 얻었다는 뜻이 아니다.
        from mini_ai_agent.conversation import ConversationState, Horizon
        rows = [{"period_end": f"{year}-12-31", "values": {"Total Revenue": year}}
                for year in range(2020, 2025)]
        raw = {"status": "partial", "statements": {
            "yearly": {name: rows for name in ("income", "balance", "cashflow")},
            "quarterly": {name: [{"period_end": f"2025-{month:02d}-01", "values": {}} for month in range(1, 9)]
                          for name in ("income", "balance", "cashflow")}}}
        resources = {"financials": raw, "_financials_original": raw}
        state = ConversationState()
        state.horizon = Horizon.MEDIUM
        view = OpenAIConversation.scoped_resources(state, resources)
        self.assertNotIn("_financials_original", view)
        self.assertEqual(len(view["financials"]["statements"]["yearly"]["income"]), 3)
        self.assertEqual(view["financials"]["status"], "ok")
        self.assertEqual(len(raw["statements"]["yearly"]["income"]), 5)
        state.horizon = Horizon.SHORT
        short = OpenAIConversation.scoped_resources(state, resources)["financials"]
        self.assertEqual(short["statements"]["yearly"]["income"], [])
        self.assertEqual(len(short["statements"]["quarterly"]["income"]), 4)
        state.horizon = Horizon.LONG
        self.assertEqual(len(OpenAIConversation.scoped_resources(state, resources)["financials"]["statements"]["yearly"]["income"]), 5)

    # HTTP 응답을 가짜로 만들되 실제 OpenAI SDK의 응답 파싱 경로는 통과한다.
    # 따라서 키/요금 없이 도구 반복, 구조화 응답, 상태 보존 조건을 테스트할 수 있다.
    def setUp(self):
        # 단위 테스트는 뉴스·가격 공급원에도 실제 요청을 보내지 않는다.
        patcher = patch("mini_ai_agent.llm.news", return_value={"status": "ok", "items": []})
        self.bundle = patcher.start()
        self.addCleanup(patcher.stop)
        finances = patch("mini_ai_agent.llm.financials", return_value={"status": "unavailable", "statements": {}})
        self.finances = finances.start()
        self.addCleanup(finances.stop)
        for name, result in [("price_history", {"status": "unavailable", "bars": []}),
                             ("analyze", {"status": "unavailable"}),
                             ("earnings_events", {"status": "ok", "items": []})]:
            mocker = patch("mini_ai_agent.llm." + name, return_value=result)
            setattr(self, name, mocker.start())
            self.addCleanup(mocker.stop)
    def make_client(self, outputs, status=200):
        # SDK는 그대로 사용하고 HTTP 전송만 가로채 준비한 응답을 순서대로 반환한다.
        self.requests = []

        def handle(request):
            self.requests.append(json.loads(request.content))
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "secret-value", "type": "invalid_request_error"}})
            return httpx.Response(200, json={"id": "resp_test", "object": "response", "created_at": 0,
                                           "status": "completed", "model": "gpt-4.1-mini",
                                           "output": outputs.pop(0)})

        client = OpenAI(api_key="test-only", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(client.close)
        return client

    def test_tool_round_trip_and_followup_history(self):
        # 검색 → 상태 저장 → 기간 질문 → 후속 답변의 상태 갱신 순서까지 재현한다.
        client = self.make_client([
            [tool("search_stock", {"query": "삼전"}, "c1")],
            [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": None}, "c2")],
            [answer("어떤 투자 기간으로 볼까요?")],
            [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "장기"}, "c3")],
            queries(),
            [answer("장기 조사 기준을 안내할게요.")],
        ])
        conversation = OpenAIConversation(client)
        self.assertIn("기간", conversation.respond("삼전 분석해줘"))
        self.assertEqual(conversation.state.code, "005930")
        self.assertEqual(conversation.state.stage, "awaiting_horizon")
        self.assertEqual([call["name"] for call in conversation.tool_calls],
                         ["search_stock", "set_research_context"])
        self.assertEqual(conversation.tool_calls[0]["arguments"], {"query": "삼전"})
        self.assertEqual(conversation.tool_calls[0]["call_id"], "c1")
        self.assertGreaterEqual(conversation.tool_calls[0]["duration_ms"], 0)
        outputs = [item for item in self.requests[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(json.loads(outputs[0]["output"])["stocks"][0]["code"], "005930")
        self.assertIn("장기", conversation.respond("장기"))
        self.assertEqual(conversation.state.stage, "ready_for_research")
        self.assertEqual(len(conversation.tool_calls), 4)
        self.assertIn("삼전 분석해줘", json.dumps(self.requests[-1], ensure_ascii=False))

    @patch("mini_ai_agent.llm.settings", return_value=("aaa", "gpt-4.1-mini"))
    def test_placeholder_does_not_call_openai(self, settings):
        # 예시 키를 실제 서비스에 보내지 않는지 확인한다.
        with patch("mini_ai_agent.llm.OpenAI") as constructor:
            with self.assertRaises(ChatError):
                OpenAIConversation().respond("안녕")
            constructor.assert_not_called()

    def test_tool_definitions_follow_context_prerequisites(self):
        from mini_ai_agent.conversation import Horizon
        conversation = OpenAIConversation()
        names = lambda expected=None: {item["name"] for item in conversation.callable_tools(conversation.state, expected)}
        self.assertEqual(names(), {"search_stock", "set_research_context"})
        conversation.state.code, conversation.state.exchange = "005930", "KOSPI"
        conversation.state.horizon = Horizon.MEDIUM
        self.assertIn("get_financials", names())
        self.assertNotIn("get_events", names())
        self.assertEqual(names(["005930", "KOSPI", "단기"]), {"search_stock", "set_research_context"})
        self.assertEqual(names(["AAPL", "NASDAQ", "중장기"]), {"search_stock", "set_research_context"})
        conversation.state.horizon = Horizon.SHORT
        self.assertIn("get_events", names(["005930", "KOSPI", "단기"]))

    def test_unadvertised_fetch_before_horizon_is_not_executed(self):
        conversation = OpenAIConversation(self.make_client([
            [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": None}, "ctx")],
            [tool("get_news", {}, "bad")],
            [answer("어떤 관점인가요?")],
        ]))
        conversation.respond("삼전 어때?")
        self.bundle.assert_not_called()
        self.assertTrue(all(not any(tool["name"].startswith("get_") for tool in request["tools"])
                            for request in self.requests))
        self.assertIn("error", conversation.tool_calls[-1]["result"])

    def test_pending_short_context_blocks_old_context_queries(self):
        from mini_ai_agent.conversation import Horizon
        conversation = OpenAIConversation(self.make_client([
            [tool("get_events", {}, "premature")],
            [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "단기"}, "ctx")],
            queries(True), [answer("단기 분석")],
        ]))
        conversation.state.code, conversation.state.exchange = "005930", "KOSPI"
        conversation.state.horizon = Horizon.MEDIUM
        conversation.respond("단기로는?")
        self.earnings_events.assert_called_once()
        self.assertEqual(self.earnings_events.call_args.args[1], "단기")
        self.assertIn("error", conversation.tool_calls[0]["result"])
        self.assertNotIn("get_events", {item["name"] for item in self.requests[0]["tools"]})
        self.assertIn("get_events", {item["name"] for item in self.requests[2]["tools"]})

    def test_each_horizon_requires_its_own_financials(self):
        # 동일 종목이어도 관점이 바뀌면 요청 재무 범위가 4분기/3년/5년으로 바뀌어야 한다.
        from mini_ai_agent.market_data import policy
        outputs = []
        for i, horizon in enumerate(["단기", "중기", "장기"]):
            outputs.extend([
                [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": horizon}, f"ctx{i}")],
                queries(horizon == "단기"),
                [answer(horizon)],
            ])
        self.finances.side_effect = lambda stock, horizon: {
            "status": "partial", "statements": {},
            "requested": {key: policy(horizon)[key] for key in ("annual_years", "quarters")},
        }
        conversation = OpenAIConversation(self.make_client(outputs))
        for horizon, years, quarters in [("단기", 0, 4), ("중기", 3, 8), ("장기", 5, 8)]:
            self.assertEqual(conversation.respond(horizon), horizon)
            self.assertEqual(conversation.research["financials"]["requested"],
                             {"annual_years": years, "quarters": quarters})
            self.assertNotIn("tool_choice", self.requests[-1])
            self.assertEqual(len(conversation.tool_calls), 5 if horizon == "단기" else 4)
            outputs = [item for item in self.requests[-1]["input"] if item.get("type") == "function_call_output"]
            self.assertIn("required_tools", json.loads(outputs[0]["output"]))
        self.assertEqual([call.args[1] for call in self.finances.call_args_list], ["단기", "중장기", "장기"])

    def test_missing_queries_are_returned_to_model(self):
        # 성급한 초안을 거절하고 모델이 조회하도록 되돌린다.
        context = tool("set_research_context", {"code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"}, "c1")
        conversation = OpenAIConversation(self.make_client([[context], [answer("초안")], queries(), [answer("조회 완료")]]))
        self.assertEqual(conversation.respond("애플 장기"), "조회 완료")
        self.finances.assert_called_once()
        self.assertIn("financials", conversation.research)
        self.assertIn("필수 조회가 누락", str(self.requests[2]["input"]))

    def test_context_only_does_not_fetch_and_invalid_calls_do_not_count(self):
        conversation = OpenAIConversation()
        resources = {}
        result = conversation.execute_tool("set_research_context", json.dumps({
            "code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"
        }), conversation.state, resources)
        self.assertIn("get_financials", result["required_tools"])
        self.finances.assert_not_called()
        self.bundle.assert_not_called()
        self.price_history.assert_not_called()
        self.assertIn("error", conversation.execute_tool("get_news", '{"bad": 1}', conversation.state, resources))
        self.assertIn("error", conversation.execute_tool("get_events", '{}', conversation.state, resources))
        self.assertNotIn("attempted_tools", resources)

    def test_long_to_medium_reuses_financials_and_prices_but_refreshes_news(self):
        # 호출 횟수를 검증해 재사용이 단지 프롬프트 문구에 그치지 않는지 확인한다.
        # 뉴스는 조회 기간별 10개 제한 때문에 장기 → 중기라도 새 도구 요청을 사용한다.
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        self.finances.return_value = {"status": "partial", "statements": {}, "fetched_at": stamp}
        self.price_history.return_value = {"status": "ok", "bars": [], "fetched_at": stamp}
        context = lambda horizon: tool("set_research_context", {
            "code": "AAPL", "exchange": "NASDAQ", "horizon": horizon}, horizon)
        conversation = OpenAIConversation(self.make_client([
            [context("장기")], queries(), [answer("장기 분석")],
            [context("중기")], [tool("get_news", {}, "new_news")], [answer("기존 재무로 중기 분석")],
            [answer("보유 자료로 후속 분석")],
        ]))
        conversation.respond("애플 장기")
        conversation.respond("중기")
        self.finances.assert_called_once()
        self.price_history.assert_called_once()
        self.assertEqual(self.bundle.call_count, 2)
        self.assertIn("get_financials", conversation.research["reused_tools"])
        # 조회 순서 자체를 강제하지 않고, 재사용 목록과 부족한 자료를 정확히 전달한다.
        context_result = conversation.tool_calls[0]["result"]
        self.assertEqual(context_result["missing_tools"], ["get_news"])
        self.assertIn("get_financials", context_result["research"]["reusable_tools"])
        context_request = self.requests[3]
        self.assertNotIn("get_financials", {tool["name"] for tool in context_request["tools"]})
        after_context_request = self.requests[4]
        self.assertIn("get_financials", {tool["name"] for tool in after_context_request["tools"]})
        conversation.respond("위험을 정리해줘")
        self.assertEqual(conversation.tool_calls, [])
        self.assertIn("현재 유효한 보유 자료", self.requests[-1]["instructions"])

    def test_expired_and_insufficient_resources_are_not_reusable(self):
        # 시간 만료와 조회 범위 부족은 서로 다른 탈락 조건이다. 두 조건을 순서대로 검사한다.
        from mini_ai_agent.conversation import Horizon
        from mini_ai_agent.market_data import policy
        from datetime import datetime, timezone
        conversation = OpenAIConversation()
        conversation.state.code, conversation.state.exchange = "AAPL", "NASDAQ"
        conversation.state.horizon = Horizon.LONG
        record = {"identity": ["AAPL", "NASDAQ"], "timestamp": datetime.now(timezone.utc).timestamp(),
                  "policy": policy("중장기"), "status": "ok"}
        resources = {"data_inventory": {"get_financials": record}, "financials": {"status": "ok"}}
        self.assertEqual(conversation.reusable_tools(conversation.state, resources), [])
        record["policy"] = policy("장기")
        self.assertEqual(conversation.reusable_tools(conversation.state, resources), ["get_financials"])
        record["timestamp"] -= 3601
        conversation.prune_resources(conversation.state, resources)
        self.assertNotIn("financials", resources)

    def test_rsi_followup_preserves_samsung_and_uses_existing_evidence(self):
        # RSI는 지표 이름이면서 실제 미국 주식 티커이기도 하다. 후속 질문에서
        # 서버가 RSI 종목으로 강제 전환하면 올바른 삼성전자 답변도 12회 거절한다.
        self.price_history.return_value = {"status": "ok", "bars": []}
        self.finances.return_value = {"status": "partial", "statements": {}}
        self.analyze.return_value = {"status": "ok", "rsi14": 34.4,
                                     "rsi_divergence": {"bearish": {"status": "ok", "detected": False}}}
        context = tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "단기"}, "ctx")
        text = "현재 비교 쌍에서는 RSI 하락다이버전스가 탐지되지 않았습니다."
        followup = answer(text, [{"text": text, "source_ids": ["c2s0"]}])
        conversation = OpenAIConversation(self.make_client(
            [[context], queries(True), [answer("삼성전자 단기 분석입니다.")]] + [[followup] for _ in range(12)]))
        conversation.respond("삼성전자 단기")
        self.assertEqual(conversation.respond("하락 rsi 다이버전스 없는거아니야 ? (단기 관점)"), text)
        self.assertEqual(conversation.state.code, "005930")
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn("tool_choice", self.requests[-1])
        self.assertEqual(conversation.tool_calls, [])
        self.assertEqual(conversation.coverage[2]["status"], "declared")
        self.price_history.assert_called_once()
        self.finances.assert_called_once()

    def test_explicit_stock_and_horizon_cannot_skip_context(self):
        # 모델이 처음부터 분석 초안을 내더라도 서버가 종목/관점 저장을 먼저 요구한다.
        conversation = OpenAIConversation(self.make_client([
            [answer("근거 없는 장기 분석")],
            [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "장기"}, "c1")],
            queries(), [answer("조회 후 분석")],
        ]))
        self.assertEqual(conversation.respond("삼성전자 장기적으로 어때"), "조회 후 분석")
        self.assertEqual(self.requests[1]["tool_choice"]["name"], "set_research_context")
        self.assertNotIn("tool_choice", self.requests[2])
        self.assertEqual(len(conversation.tool_calls), 4)

    def test_answer_and_multiple_sources_are_returned_in_one_response(self):
        # 사후 평가 모델 호출 없이 답변 생성 응답 하나에서 결론+두 근거를 함께 얻는다.
        self.analyze.return_value = {'status':'ok','rsi14':34.4,'macd_12_26_9':{'line':-2}}
        context = tool('set_research_context', {'code':'005930','exchange':'KOSPI','horizon':'단기'}, 'c1')
        text = '단기 모멘텀 약화에 유의해야 합니다.'
        # 단기 기준표에서 RSI는 0번, 다이버전스 두 항목 뒤의 MACD는 3번이다.
        conversation = OpenAIConversation(self.make_client([[context], queries(True), [answer(text,[{'text':text,'source_ids':['c0s0','c3s0']}])]]))
        self.assertEqual(conversation.respond('삼성전자 단기로'),text)
        self.assertEqual(len(self.requests),3)
        self.assertEqual(conversation.coverage[0]['status'],'declared')
        self.assertEqual(conversation.coverage[3]['status'],'declared')
        self.assertEqual(conversation.coverage[1]['status'],'no_data')
        self.assertEqual(len(conversation.coverage[0]['claims'][0]['sources']),2)
        self.assertIn('34.4',self.requests[-1]['instructions'])
        self.assertEqual(self.requests[-1]['text']['format']['name'],'research_answer')
        conversation.respond('/reset')
        self.assertEqual(conversation.coverage,[])

    def test_explicit_horizon_change_cannot_use_previous_context(self):
        # 이전 중기 상태를 남긴 채 '장기' 입력을 무시하는 회귀를 막는다.
        from mini_ai_agent.conversation import Horizon
        conversation = OpenAIConversation(self.make_client([
            [answer("중기 자료로 장기 분석")],
            [tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "장기"}, "c1")],
            queries(), [answer("장기 자료 확인")],
        ]))
        conversation.state.code, conversation.state.exchange = "005930", "KOSPI"
        conversation.state.horizon = Horizon.MEDIUM
        self.assertEqual(conversation.respond("장기"), "장기 자료 확인")
        self.assertEqual(self.finances.call_args.args[1], "장기")

    def test_ignored_context_requirement_does_not_return_analysis(self):
        # 모델이 강제 지시를 계속 무시하면 제한 횟수 후 실패하고 원본 이력은 보존한다.
        conversation = OpenAIConversation(self.make_client([[answer("임의 분석")]] * 12))
        with self.assertRaises(ChatError):
            conversation.respond("삼성전자 장기적으로 어때")
        self.assertEqual(conversation.history, [])

    def test_stock_change_invalidates_previous_financials_in_same_turn(self):
        # 한 요청 안에서도 다른 종목으로 바뀌면 앞 종목의 재무를 근거로 쓰면 안 된다.
        def context(code, identifier):
            return tool("set_research_context", {"code": code, "exchange": "NASDAQ", "horizon": "장기"}, identifier)
        conversation = OpenAIConversation(self.make_client([
            [context("AAPL", "c1")], [tool("get_financials", {}, "f1")],
            [context("TSLA", "c2")], queries(), [answer("테슬라")],
        ]))
        # 목표 종목이 명시된 요청에서 엉뚱한 종목을 조회하는 검사는 별도로 한다.
        conversation.respond("두 종목을 차례로 확인해줘")
        self.assertEqual([call.args[0]["code"] for call in self.finances.call_args_list], ["AAPL", "TSLA"])
        self.assertEqual(conversation.research["financials_context_key"], ["TSLA", "NASDAQ", "장기"])

    def test_auth_error_is_safe(self):
        # 제공자의 원본 오류가 사용자에게 노출되지 않고 실패한 입력도 저장되지 않아야 한다.
        conversation = OpenAIConversation(self.make_client([], 401))
        with self.assertLogs("mini_ai_agent.llm", level="WARNING") as logs:
            with self.assertRaises(ChatError) as error:
                conversation.respond("안녕")
        self.assertNotIn("secret-value", str(error.exception))
        self.assertNotIn("secret-value", str(logs.output))
        self.assertIn("HTTP 401", str(error.exception))
        self.assertEqual(conversation.history, [])

    def test_loop_limit_rolls_back_state(self):
        # 모델이 계속 도구를 요청하면 중단하고, 중간 상태 변경을 확정하지 않아야 한다.
        call = tool("set_research_context", {"code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"}, "c1")
        conversation = OpenAIConversation(self.make_client([[call] for _ in range(12)]))
        with self.assertRaises(ChatError):
            conversation.respond("애플 장기로")
        self.assertIsNone(conversation.state.company)
        self.assertEqual(conversation.history, [])

    def test_reset_clears_history_without_api_call(self):
        # 초기화 자체에는 모델 호출이 필요하지 않다.
        conversation = OpenAIConversation(self.make_client([[answer("안녕하세요")]]))
        conversation.respond("안녕")
        self.assertEqual(conversation.tool_calls, [])
        conversation.respond("/reset")
        self.assertEqual(conversation.history, [])
        self.assertEqual(len(self.requests), 1)

    @patch("mini_ai_agent.llm.financials", return_value={"status": "partial", "statements": {}, "missing": ["test"]})
    def test_financial_tool_and_mandatory_followup_news(self, financials):
        # 동일 턴 재무 중복 호출은 재사용하지만 다음 질문에는 필수 자료를 다시 확보한다.
        context = tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "장기"}, "c1")
        client = self.make_client([[context], queries(), [answer("재무 확인")],
                                   [answer("이전 자료 답변")], queries(), [answer("최신 뉴스 확인")]])
        conversation = OpenAIConversation(client)
        conversation.respond("삼전 장기 분석")
        financials.assert_called_once()
        self.assertEqual(conversation.research["financials"]["status"], "unavailable")

        self.assertEqual(conversation.respond("뉴스는?"), "최신 뉴스 확인")
        self.assertEqual(self.bundle.call_count, 2)
        self.assertTrue(any(item.get("role") == "developer" for item in self.requests[-1]["input"]))

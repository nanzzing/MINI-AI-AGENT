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


def queries(short=False, long=True):
    # 서버가 자동 조회하는 흉내가 아니라 모델이 실제 반환할 function_call 배열을 만든다.
    # short=True일 때 일정 도구까지 포함하여 관점별 필수 자료 검사를 통과시킨다.
    names = ["get_price_analysis", "get_financials", "get_news"] + (["get_events"] if short else
             ["get_valuation_comparison"] if long else [])
    return [tool(name, {}, name) for name in names]


class LLMTests(unittest.TestCase):
    def test_industry_question_ignores_previous_short_checklist(self):
        from mini_ai_agent.conversation import Horizon
        client = self.make_client([
            [tool('set_response_mode', {'mode': 'industry'}, 'mode')],
            [tool('search_topic_news', {'query': '반도체 수요', 'days': 30}, 'news')],
            [answer('수요 전망은 추가 확인이 필요합니다.', [{'text': '수요 전망은 추가 확인이 필요합니다.', 'source_ids': ['q0']}])],
        ], include_search=False)
        session = OpenAIConversation(client)
        session.state.code, session.state.exchange, session.state.horizon = '005930', 'KOSPI', Horizon.SHORT
        with patch('mini_ai_agent.llm.topic_news', return_value={'status': 'ok', 'source': 'RSS', 'items': [{'title': '수요 전망'}]}) as fetch:
            session.respond('반도체 수요는 계속 갈까?', selection='자동')
        fetch.assert_called_once_with('반도체 수요', 30)
        self.finances.assert_not_called()
        self.assertEqual(session.research['response_mode'], 'industry')
        self.assertEqual(session.coverage[0]['status'], 'declared')
        self.assertEqual(session.coverage[0]['criterion'], '반도체 수요')

    def test_comparison_keeps_separate_verified_company_data(self):
        session = OpenAIConversation()
        resources = {}
        args = {'code': 'NVDA', 'exchange': 'NASDAQ', 'horizon': '중기', 'kind': 'financials'}
        self.assertIn('error', session.execute_tool('get_company_data', json.dumps(args), session.state, resources))
        for code in ['NVDA', 'AAPL']:
            session.execute_tool('search_stock', json.dumps({'query': code}), session.state, resources)
            args['code'] = code
            session.execute_tool('get_company_data', json.dumps(args), session.state, resources)
        self.assertIsNone(session.state.code)
        self.assertEqual(len(resources['question_sources']), 2)
        self.assertEqual(self.finances.call_count, 2)
        result = session.execute_tool('get_company_data', json.dumps(args), session.state, resources)
        self.assertTrue(result['reused'])
        self.assertEqual(self.finances.call_count, 2)
        args['horizon'] = '장기'
        session.execute_tool('get_company_data', json.dumps(args), session.state, resources)
        self.assertEqual(self.finances.call_count, 3)
        for entry in resources['question_sources'].values():
            entry['timestamp'] = 0
        self.assertFalse(session.scoped_resources(session.state, resources)['question_sources'])

    def test_general_tools_reject_invalid_scope(self):
        session = OpenAIConversation()
        resources = {}
        for args in [{'query': '', 'days': 30}, {'query': '반도체', 'days': 365}]:
            self.assertIn('error', session.execute_tool('search_topic_news', json.dumps(args), session.state, resources))
        session.execute_tool('search_stock', '{"query":"NVDA"}', session.state, resources)
        args = {'code': 'NVDA', 'exchange': 'NASDAQ', 'horizon': '단기', 'kind': 'valuation'}
        self.assertIn('error', session.execute_tool('get_company_data', json.dumps(args), session.state, resources))
        self.valuation.assert_not_called()

    def test_new_stock_requires_search_in_this_session(self):
        session = OpenAIConversation()
        resources = {}
        args = json.dumps({'code': 'NVDA', 'exchange': 'NASDAQ', 'horizon': '장기'})
        result = session.execute_tool('set_research_context', args, session.state, resources)
        self.assertIn('error', result)
        self.assertIsNone(session.state.code)
        self.finances.assert_not_called()
        with patch('mini_ai_agent.llm.search_stocks', return_value={'stocks': []}):
            session.execute_tool('search_stock', '{"query":"unknown"}', session.state, resources)
        self.assertIn('error', session.execute_tool('set_research_context', args, session.state, resources))

    def test_model_resolves_typo_through_search_round_trip(self):
        # 오타 해석 자체는 모델의 책임이다. 서버는 해석된 검색어의 결과만 승인한다.
        client = self.make_client([
            [tool('search_stock', {'query': 'NVDA'}, 'search')],
            [tool('set_research_context', {'code': 'NVDA', 'exchange': 'NASDAQ', 'horizon': '장기'}, 'context')],
            queries(), [answer('엔비디아(NVDA) 기준으로 확인했습니다.')],
        ], include_search=False)
        session = OpenAIConversation(client)
        session.respond('앤비디아 장기적으로 어때?')
        self.assertEqual(session.state.code, 'NVDA')
        self.assertEqual(session.tool_calls[0]['name'], 'search_stock')
        outputs = [item for item in self.requests[1]['input'] if item.get('type') == 'function_call_output']
        self.assertEqual(json.loads(outputs[0]['output'])['stocks'][0]['code'], 'NVDA')

    def test_search_candidates_survive_clarification_and_reset(self):
        client = self.make_client([
            [tool('search_stock', {'query': 'NVDA'}, 'search')],
            [answer('엔비디아를 말씀하신 건가요?')],
            [tool('set_research_context', {'code': 'NVDA', 'exchange': 'NASDAQ', 'horizon': None}, 'context')],
            [answer('어떤 투자 기간으로 볼까요?')],
        ], include_search=False)
        session = OpenAIConversation(client)
        session.respond('앤비디아?')
        session.respond('응 맞아')
        self.assertEqual(session.state.code, 'NVDA')
        self.assertNotIn('search_stock', [call['name'] for call in session.tool_calls])
        session.respond('/reset')
        self.assertEqual(session.saved_resources, {})

    def test_explanation_keeps_previous_answer_without_full_research(self):
        from mini_ai_agent.conversation import Horizon
        client = self.make_client([
            [tool('set_response_mode', {'mode': 'explain'}, 'mode1')],
            [answer('이전 답변은 당시 자료에 근거한 판단입니다.')],
        ])
        session = OpenAIConversation(client=client)
        session.state.code, session.state.exchange, session.state.horizon = '005930', 'KOSPI', Horizon.SHORT
        session.dialogue = [{'question': '삼성전자 단기', 'answer': '반등 확인이 필요합니다.',
                             'context': {}, 'answered_at': '2026-01-01', 'evidence': []}]
        session.respond('왜 그렇게 생각해?')
        self.assertIn('반등 확인이 필요합니다.', self.requests[0]['instructions'])
        self.assertEqual([row['name'] for row in session.tool_calls], ['set_response_mode'])
        self.finances.assert_not_called()
        self.assertEqual(len(session.dialogue), 2)

    def test_concept_without_stock_or_horizon(self):
        client = self.make_client([
            [tool('set_response_mode', {'mode': 'concept'}, 'mode1')],
            [answer('RSI는 상대강도지수입니다.')],
        ])
        session = OpenAIConversation(client=client)
        self.assertIn('상대강도', session.respond('RSI가 뭐야?'))
        self.assertIsNone(session.state.horizon)
        self.price_history.assert_not_called()
        session.respond('/reset')
        self.assertEqual(session.dialogue, [])

    def test_response_mode_validation(self):
        session = OpenAIConversation()
        self.assertIn('error', session.execute_tool('set_response_mode', '{"mode":"skip"}', session.state, {}))

    def test_horizon_change_still_requires_research(self):
        from mini_ai_agent.conversation import Horizon
        client = self.make_client([
            [tool('set_response_mode', {'mode': 'focused'}, 'mode1')],
            [tool('set_research_context', {'code': '005930', 'exchange': 'KOSPI', 'horizon': '장기'}, 'ctx')],
            [answer('성급한 초안')],
            queries(),
            [answer('장기 자료를 확인했습니다.')],
        ])
        session = OpenAIConversation(client=client)
        session.state.code, session.state.exchange, session.state.horizon = '005930', 'KOSPI', Horizon.SHORT
        self.assertEqual(session.respond('그럼 장기는?'), '장기 자료를 확인했습니다.')
        self.finances.assert_called_once()
        self.valuation.assert_called_once()
        self.assertTrue(any(row['event'] == '서버 보완 요청' for row in session.agent_steps))

    def test_focused_mode_expires_after_turn_and_memory_is_bounded(self):
        from mini_ai_agent.conversation import Horizon
        client = self.make_client([
            [tool('set_response_mode', {'mode': 'focused'}, 'mode1')],
            [answer('확인할 최신 자료가 없습니다.')],
            [answer('성급한 초안')], queries(short=True), [answer('전체 분석입니다.')],
        ])
        session = OpenAIConversation(client=client)
        session.state.code, session.state.exchange, session.state.horizon = '005930', 'KOSPI', Horizon.SHORT
        session.dialogue = [{'question': str(i), 'answer': '과거 답변', 'evidence': []} for i in range(4)]
        session.respond('RSI만 설명해줘')
        self.finances.assert_not_called()
        self.assertEqual(len(session.dialogue), 4)
        session.respond('다시 전체 분석해줘')
        self.finances.assert_called_once()
        self.assertEqual(len(session.dialogue), 4)

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
        valuation = patch('mini_ai_agent.llm.valuation_comparison', return_value={'status': 'unavailable', 'metrics': {}})
        self.valuation = valuation.start()
        self.addCleanup(valuation.stop)
        for name, result in [("price_history", {"status": "unavailable", "bars": []}),
                             ("analyze", {"status": "unavailable"}),
                             ("earnings_events", {"status": "ok", "items": []})]:
            mocker = patch("mini_ai_agent.llm." + name, return_value=result)
            setattr(self, name, mocker.start())
            self.addCleanup(mocker.stop)
    def make_client(self, outputs, status=200, *, include_search=True):
        # SDK는 그대로 사용하고 HTTP 전송만 가로채 준비한 응답을 순서대로 반환한다.
        # 재무/재사용 중심의 기존 시나리오는 새 종목 앞에 검색 요청을 추가한다.
        # 식별 절차 자체를 검증하는 테스트는 include_search=False로 모든 왕복을 명시한다.
        if include_search:
            from mini_ai_agent.stocks import find_stocks
            verified = set()
            for output in outputs:
                if not isinstance(output, list):
                    continue
                prepared = []
                for call in output:
                    if call.get('name') == 'search_stock':
                        verified.update(row['code'] for row in find_stocks(json.loads(call['arguments'])['query']))
                    if call.get('name') == 'set_research_context':
                        code = json.loads(call['arguments'])['code']
                        if code not in verified:
                            prepared.append(tool('search_stock', {'query': code}, 'verify_' + call['id']))
                            verified.add(code)
                    prepared.append(call)
                output[:] = prepared
        self.requests = []

        def handle(request):
            self.requests.append(json.loads(request.content))
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "secret-value", "type": "invalid_request_error"}})
            output = outputs.pop(0)
            if isinstance(output, Exception):
                raise output
            return httpx.Response(200, json={"id": "resp_test", "object": "response", "created_at": 0,
                                           "status": "completed", "model": "gpt-4.1-mini",
                                           "output": output})

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
        self.assertEqual(len(conversation.tool_calls), 5)
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
        base = {"search_stock", "set_research_context", "set_response_mode", "search_topic_news", "get_company_data"}
        self.assertEqual(names(), base)
        conversation.state.code, conversation.state.exchange = "005930", "KOSPI"
        conversation.state.horizon = Horizon.MEDIUM
        self.assertIn("get_financials", names())
        self.assertNotIn("get_events", names())
        self.assertEqual(names(["005930", "KOSPI", "단기"]), base)
        self.assertEqual(names(["AAPL", "NASDAQ", "중장기"]), base)
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
        self.assertTrue(all(not any(tool["name"] == "get_news" for tool in request["tools"])
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
                queries(horizon == "단기", long=horizon == "장기"),
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
            self.assertEqual(len(conversation.tool_calls), 4 if horizon == "중기" else 6 if horizon == '단기' else 5)
            outputs = [item for item in self.requests[-1]["input"] if item.get("type") == "function_call_output"]
            self.assertTrue(any('required_tools' in json.loads(output['output']) for output in outputs))
        self.assertEqual([call.args[1] for call in self.finances.call_args_list], ["단기", "중장기", "장기"])

    def test_missing_queries_are_returned_to_model(self):
        # 성급한 초안을 거절하고 모델이 조회하도록 되돌린다.
        context = tool("set_research_context", {"code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"}, "c1")
        conversation = OpenAIConversation(self.make_client([[context], [answer("초안")], queries(), [answer("조회 완료")]]))
        self.assertEqual(conversation.respond("애플 장기"), "조회 완료")
        self.finances.assert_called_once()
        self.assertIn("financials", conversation.research)
        self.assertIn("필수 조회가 누락", str(self.requests[2]["input"]))
        self.assertEqual(self.requests[2]['tool_choice'], {'type': 'function', 'name': 'get_price_analysis'})
        self.assertNotIn('tool_choice', self.requests[3])

    def test_repair_fetches_each_missing_resource_then_returns_answer(self):
        context = tool('set_research_context', {'code': 'AAPL', 'exchange': 'NASDAQ', 'horizon': '장기'}, 'context')
        conversation = OpenAIConversation(self.make_client(
            [[context], [answer('자료 없는 초안')]] + [[call] for call in queries()] + [[answer('자료 확인 완료')]]))
        self.assertEqual(conversation.respond('애플 장기'), '자료 확인 완료')
        self.assertEqual([request['tool_choice']['name'] for request in self.requests[2:6]],
                         ['get_price_analysis', 'get_financials', 'get_news', 'get_valuation_comparison'])
        self.assertNotIn('tool_choice', self.requests[-1])

    def test_ignored_required_call_is_not_retried_as_another_draft(self):
        context = tool('set_research_context', {'code': 'AAPL', 'exchange': 'NASDAQ', 'horizon': '장기'}, 'context')
        conversation = OpenAIConversation(self.make_client([[context], [answer('초안')], [answer('같은 초안')]]))
        with self.assertRaisesRegex(ChatError, 'get_price_analysis'):
            conversation.respond('애플 장기')
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(conversation.history, [])

    def test_context_only_does_not_fetch_and_invalid_calls_do_not_count(self):
        conversation = OpenAIConversation()
        resources = {}
        conversation.execute_tool('search_stock', '{"query":"AAPL"}', conversation.state, resources)
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
        # 서버가 RSI 종목으로 강제 전환하면 올바른 삼성전자 답변도 계속 거절하게 된다.
        self.price_history.return_value = {"status": "ok", "bars": []}
        self.finances.return_value = {"status": "partial", "statements": {}}
        self.analyze.return_value = {"status": "ok", "rsi14": 34.4,
                                     "rsi_divergence": {"bearish": {"status": "ok", "detected": False}}}
        context = tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "단기"}, "ctx")
        text = "현재 비교 쌍에서는 RSI 하락다이버전스가 탐지되지 않았습니다."
        followup = answer(text, [{"text": text, "source_ids": ["c3s0"]}])
        conversation = OpenAIConversation(self.make_client(
            [[context], queries(True), [answer("삼성전자 단기 분석입니다.")]] + [[followup] for _ in range(12)]))
        conversation.respond("삼성전자 단기")
        self.assertEqual(conversation.respond("하락 rsi 다이버전스 없는거아니야 ? (단기 관점)"), text)
        self.assertEqual(conversation.state.code, "005930")
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn("tool_choice", self.requests[-1])
        self.assertEqual(conversation.tool_calls, [])
        self.assertEqual(conversation.coverage[3]["status"], "declared")
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
        self.assertEqual(self.requests[1]["tool_choice"]["name"], "search_stock")
        self.assertNotIn("tool_choice", self.requests[2])
        self.assertEqual(len(conversation.tool_calls), 6)

    def test_answer_and_multiple_sources_are_returned_in_one_response(self):
        # 사후 평가 모델 호출 없이 답변 생성 응답 하나에서 결론+두 근거를 함께 얻는다.
        self.analyze.return_value = {'status':'ok','rsi14':34.4,'macd_12_26_9':{'line':-2}}
        context = tool('set_research_context', {'code':'005930','exchange':'KOSPI','horizon':'단기'}, 'c1')
        text = '단기 모멘텀 약화에 유의해야 합니다.'
        # 차트 항목이 맨 앞으로 오면서 RSI는 1번, MACD는 4번이다.
        conversation = OpenAIConversation(self.make_client([[context], queries(True), [answer(text,[{'text':text,'source_ids':['c1s0','c4s0']}])]]))
        self.assertEqual(conversation.respond('삼성전자 단기로'),text)
        self.assertEqual(len(self.requests),3)
        self.assertEqual(conversation.coverage[1]['status'],'declared')
        self.assertEqual(conversation.coverage[4]['status'],'declared')
        self.assertEqual(conversation.coverage[2]['status'],'no_data')
        self.assertEqual(len(conversation.coverage[1]['claims'][0]['sources']),2)
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
        # 횟수 제한이 없어도 틀린 종목/관점의 초안은 공개하지 않는다.
        # 강제한 도구 요청까지 무시하면 같은 초안을 반복 요청하지 않는다.
        conversation = OpenAIConversation(self.make_client(
            [[answer("임의 분석")]] * 13 + [httpx.ReadTimeout("test timeout")]))
        with self.assertRaises(ChatError):
            conversation.respond("삼성전자 장기적으로 어때")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(conversation.history, [])
        self.assertIsNone(conversation.state.company)

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

    def test_transport_error_after_many_rounds_rolls_back_state(self):
        # 긴 조사도 허용하지만 통신 실패 시에는 중간 상태 변경을 확정하지 않는다.
        call = tool("set_research_context", {"code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"}, "c1")
        conversation = OpenAIConversation(self.make_client(
            [[call] for _ in range(13)] + [httpx.ReadTimeout("test timeout")]))
        with self.assertRaises(ChatError):
            conversation.respond("애플 장기로")
        self.assertEqual(len(self.requests), 14)
        self.assertIsNone(conversation.state.company)
        self.assertEqual(conversation.history, [])

    def test_more_than_twelve_model_rounds_can_complete(self):
        # 기존 12회 왕복 제한을 넘어도 최종 답변까지 가며 회차 기록은 정확해야 한다.
        context = tool("set_research_context", {"code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"}, "ctx")
        conversation = OpenAIConversation(self.make_client(
            [[tool('search_stock', {'query': '애플'}, f'search{i}')] for i in range(13)]
            + [[context], queries(), [answer("장기 조사 완료")]]))
        self.assertEqual(conversation.respond("애플 장기"), "장기 조사 완료")
        rounds = [step["round"] for step in conversation.agent_steps if step["event"] == "모델 호출"]
        self.assertEqual(rounds, list(range(1, 17)))
        self.assertEqual(conversation.state.code, "AAPL")
        self.assertEqual(conversation.agent_steps[-1]["round"], 16)

    def test_more_than_twelve_tool_calls_can_complete(self):
        # 한 모델 응답의 도구 요청 여러 개도 합계 12개에서 끊지 않는다.
        calls = [tool("search_stock", {"query": "애플"}, f"search{i}") for i in range(14)]
        conversation = OpenAIConversation(self.make_client([calls, [answer("종목 검색 완료")]]))
        self.assertEqual(conversation.respond("종목 목록을 확인해줘"), "종목 검색 완료")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(conversation.tool_calls), 14)
        results = [item for item in self.requests[-1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in results], [call["call_id"] for call in calls])
        self.assertTrue(all(call["round"] == 1 for call in conversation.tool_calls))

    def test_progress_arrives_before_work_and_preserves_final_timings(self):
        events = []
        client = self.make_client([
            [tool('search_stock', {'query': '삼전'}, 'search')], [answer('관점을 알려주세요.')]])
        conversation = OpenAIConversation(client)
        create, execute = client.responses.create, conversation.execute_tool

        def model_request(**kwargs):
            self.assertEqual(events[-1]['event'], '모델 호출')
            self.assertEqual(conversation.history, [])
            return create(**kwargs)

        def execute_tool(*args):
            self.assertEqual(events[-1]['event'], '모델 → 파이썬')
            self.assertEqual(events[-1]['tool'], args[0])
            return execute(*args)

        with patch.object(client.responses, 'create', side_effect=model_request), \
             patch.object(conversation, 'execute_tool', side_effect=execute_tool):
            conversation.respond('종목 검색해줘', on_progress=events.append)
        self.assertEqual(events, conversation.agent_steps)
        self.assertEqual(events[-1]['event'], '최종 답변')
        self.assertEqual([e['elapsed_ms'] for e in events], sorted(e['elapsed_ms'] for e in events))
        measured = [e for e in events if e['event'] in {'모델 응답 수신', '파이썬 실행 완료'}]
        self.assertEqual(len(measured), 3)
        self.assertTrue(all(e['duration_ms'] >= 0 for e in measured))
        self.assertTrue(all('arguments' not in e and 'result' not in e for e in events))

    def test_progress_disconnect_prevents_next_tool_and_rolls_back(self):
        conversation = OpenAIConversation(self.make_client([
            [tool('search_stock', {'query': '삼전'}, 'search')]]))

        def disconnected(step):
            if step['event'] == '모델 → 파이썬':
                raise ConnectionError('browser disconnected')

        with patch.object(conversation, 'execute_tool') as execute:
            with self.assertRaises(ConnectionError):
                conversation.respond('종목 검색해줘', on_progress=disconnected)
        execute.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(conversation.history, [])

    def test_common_rules_are_in_prompt_not_checklist(self):
        from mini_ai_agent.conversation import RESEARCH_FOCUS
        conversation = OpenAIConversation(self.make_client([[answer('안녕하세요.')]]))
        conversation.respond('안녕')
        instructions = self.requests[0]['instructions']
        for rule in ('상승 근거와 반대 근거', '독립적인 증거처럼 중복 계산하지 않는다',
                     '자료 부족·판단 불가·부정적 결과', '회사 가이던스·시장 예상치·모델의 가정',
                     '출처·기준일·분석 기간', '업종과 사업 특성', '수익이나 손실 한도를 보장하지 않는다',
                     '최근 20거래일을 우선하고 최대 40거래일까지 확인'):
            self.assertIn(rule, instructions)
            self.assertTrue(all(rule not in item for group in RESEARCH_FOCUS.values() for item in group))

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

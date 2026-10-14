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


def answer(text):
    """도구 호출이 끝난 뒤의 최종 텍스트 응답을 재현한다."""
    return {"type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


class LLMTests(unittest.TestCase):
    def setUp(self):
        # 단위 테스트는 뉴스·가격 공급원에도 실제 요청을 보내지 않는다.
        patcher = patch("mini_ai_agent.llm.research_bundle", return_value={"news": {"status": "ok", "items": []}})
        self.bundle = patcher.start()
        self.addCleanup(patcher.stop)
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
            [answer("장기 조사 기준을 안내할게요.")],
        ])
        conversation = OpenAIConversation(client)
        self.assertIn("기간", conversation.respond("삼전 분석해줘"))
        self.assertEqual(conversation.state.code, "005930")
        self.assertEqual(conversation.state.stage, "awaiting_horizon")
        outputs = [item for item in self.requests[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(json.loads(outputs[0]["output"])["stocks"][0]["code"], "005930")
        self.assertIn("장기", conversation.respond("장기"))
        self.assertEqual(conversation.state.stage, "ready_for_research")
        self.assertIn("삼전 분석해줘", json.dumps(self.requests[-1], ensure_ascii=False))

    @patch("mini_ai_agent.llm.settings", return_value=("aaa", "gpt-4.1-mini"))
    def test_placeholder_does_not_call_openai(self, settings):
        # 예시 키를 실제 서비스에 보내지 않는지 확인한다.
        with patch("mini_ai_agent.llm.OpenAI") as constructor:
            with self.assertRaises(ChatError):
                OpenAIConversation().respond("안녕")
            constructor.assert_not_called()

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
        conversation = OpenAIConversation(self.make_client([[call] for _ in range(6)]))
        with self.assertRaises(ChatError):
            conversation.respond("애플 장기로")
        self.assertIsNone(conversation.state.company)
        self.assertEqual(conversation.history, [])

    def test_reset_clears_history_without_api_call(self):
        # 초기화 자체에는 모델 호출이 필요하지 않다.
        conversation = OpenAIConversation(self.make_client([[answer("안녕하세요")]]))
        conversation.respond("안녕")
        conversation.respond("/reset")
        self.assertEqual(conversation.history, [])
        self.assertEqual(len(self.requests), 1)

    @patch("mini_ai_agent.llm.financials", return_value={"status": "partial", "statements": {}, "missing": ["test"]})
    def test_financial_tool_and_mandatory_followup_news(self, financials):
        context = tool("set_research_context", {"code": "005930", "exchange": "KOSPI", "horizon": "장기"}, "c1")
        client = self.make_client([[context], [tool("get_financials", {}, "c2")], [answer("재무 확인")],
                                   [answer("이전 자료 답변")], [answer("최신 뉴스 확인")]])
        conversation = OpenAIConversation(client)
        conversation.respond("삼전 장기 분석")
        financials.assert_called_once()
        self.assertEqual(conversation.research["financials"]["status"], "partial")
        self.assertEqual(conversation.respond("뉴스는?"), "최신 뉴스 확인")
        self.assertEqual(self.bundle.call_count, 2)
        self.assertTrue(any(item.get("role") == "developer" for item in self.requests[-1]["input"]))

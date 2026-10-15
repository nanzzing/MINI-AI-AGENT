"""모델 연결 전의 규칙 기반 대화에서 종목·기간 확인 순서를 검증한다."""

import unittest

from mini_ai_agent.conversation import Horizon, InvestmentConversation, detect_horizon


class ConversationTests(unittest.TestCase):
    # 실제 웹 모델 테스트가 아니라 초기 규칙 기반 예제의 동작 계약을 보존하는 테스트다.
    def test_followup_preserves_company(self):
        # 첫 질문에서 종목을 기억하고 다음 메시지에서는 기간만 받아 완성한다.
        conversation = InvestmentConversation()
        self.assertIn("투자 기간", conversation.respond("삼전 어때?"))
        self.assertEqual(conversation.state.stage, "awaiting_horizon")
        answer = conversation.respond("장기로 보고 있어")
        self.assertEqual(conversation.state.company, "삼성전자")
        self.assertEqual(conversation.state.horizon, Horizon.LONG)
        self.assertEqual(conversation.state.stage, "ready_for_research")
        self.assertIn("사업 경쟁력", answer)

    def test_explicit_horizon_skips_question(self):
        # 한 메시지에 종목과 기간이 있으면 기간을 다시 묻지 않는다.
        conversation = InvestmentConversation()
        conversation.respond("삼성전자 중장기로 어때?")
        self.assertEqual(conversation.state.horizon, Horizon.MEDIUM)
        self.assertEqual(conversation.state.stage, "ready_for_research")

    def test_ambiguous_horizon_needs_clarification(self):
        # 두 기간을 언급하면 하나를 임의 선택하지 않는다.
        self.assertIsNone(detect_horizon("단기 또는 장기"))

    def test_period_before_company_and_reset(self):
        # 기간을 먼저 정하는 경우와 새 대화에서 이전 선택을 지우는 경우를 함께 확인한다.
        conversation = InvestmentConversation()
        conversation.respond("단기")
        self.assertEqual(conversation.state.stage, "awaiting_company")
        self.assertIn("최근 주가", conversation.respond("삼전"))
        conversation.respond("/reset")
        self.assertIsNone(conversation.state.company)
        self.assertIsNone(conversation.state.horizon)


if __name__ == "__main__":
    unittest.main()

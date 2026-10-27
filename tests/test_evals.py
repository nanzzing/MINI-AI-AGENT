"""평가기 자체의 오판을 방지한다. 실제 모델 호출 없이 합격/실패 양쪽을 검증한다."""
import sys
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from fixtures import fixed_data, finances
from run import grade, print_result
from mini_ai_agent.llm import OpenAIConversation
from mini_ai_agent.conversation import Horizon


class EvaluationTests(unittest.TestCase):
    def test_failure_display_preserves_score_and_reports_errors(self):
        row = {"case": "sample", "turn": 1, "passed": False,
               "checks": {"stock": True, "reuse_financials": False, "no_tool_errors": False},
               "tools": [{"name": "get_events", "result": {"error": "단기 설정 필요"}}]}
        output = StringIO()
        with redirect_stdout(output):
            print_result(row)
        self.assertIn("FAIL reuse_financials", output.getvalue())
        self.assertIn("get_events: 단기 설정 필요", output.getvalue())
        self.assertNotIn("FAIL stock", output.getvalue())
        self.assertFalse(row["passed"])

    def test_scope_grader_rejects_excess_periods(self):
        agent = OpenAIConversation()
        agent.state.code, agent.state.horizon = "AAPL", Horizon.MEDIUM
        expected = {"code": "AAPL", "horizon": "중장기", "finance": [3, 8]}
        agent.research = {"financials": finances({}, "중장기", "complete")}
        self.assertTrue(all(grade(expected, agent).values()))
        agent.research = {"financials": finances({}, "장기", "complete")}
        self.assertFalse(grade(expected, agent)["finance_rows"])

    def test_fixture_tools_work_without_network(self):
        import json
        with fixed_data():
            agent = OpenAIConversation()
            resources = {}
            agent.execute_tool('search_stock', '{"query":"AAPL"}', agent.state, resources)
            agent.execute_tool("set_research_context", json.dumps({"code": "AAPL", "exchange": "NASDAQ", "horizon": "장기"}), agent.state, resources)
            result = agent.execute_tool("get_financials", "{}", agent.state, resources)
            self.assertEqual(result["status"], "ok")
            self.assertIn("get_financials", agent.reusable_tools(agent.state, resources))
            valuation = agent.execute_tool('get_valuation_comparison', '{}', agent.state, resources)
            self.assertEqual(valuation['source'], 'SYNTHETIC EVALUATION FIXTURE')

    def test_fixture_partial_and_unavailable(self):
        self.assertEqual(finances({}, "장기", "partial")["status"], "partial")
        self.assertEqual(finances({}, "장기", "unavailable")["statements"], {})

"""실제 모델 행동 평가. 기본은 설정 검증만 수행하며 --live에서만 API 비용이 발생한다."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fixtures import fixed_data
from mini_ai_agent.llm import OpenAIConversation, PROMPT, settings, ChatError


CHECK_LABELS = {
    "stock": "종목 일치", "horizon": "투자 관점 일치",
    "no_unnecessary_fetch": "관점 미정에서 조회를 요청하지 않음",
    "reuse_financials": "유효한 기존 재무를 재호출 없이 사용 가능하게 유지",
    "fetch_financials": "필요한 재무 신규 조회", "finance_scope": "재무 요청 범위",
    "finance_rows": "실제 재무 기간 수", "finance_status": "재무 제공 상태",
    "no_tool_errors": "도구 입력·실행 오류 없음",
}


def print_result(row):
    """기존 보고서도 다시 채점하지 않고 당시 실패 조건을 설명한다. API 호출은 없다."""
    print(row["case"], row["turn"], "PASS" if row["passed"] else "FAIL")
    for name, passed in row.get("checks", {}).items():
        if not passed:
            print(f"  FAIL {name}: {CHECK_LABELS.get(name, name)}")
    if row.get("error"):
        print("  실행 오류: " + row["error"])
    if not row["passed"] and row.get("tools"):
        print("  도구 순서: " + " -> ".join(call["name"] for call in row["tools"]))
        for call in row["tools"]:
            if call.get("result", {}).get("error"):
                print(f"  {call['name']}: {call['result']['error']}")


def grade(expected, conversation):
    """문장/도구 순서를 고정하지 않는다. 아래 점수는 행동 계약만 검증한다."""
    state = conversation.state
    names = [call["name"] for call in conversation.tool_calls]
    checks = {"stock": state.code == expected["code"],
              "horizon": (state.horizon.value if state.horizon else None) == expected["horizon"]}
    if expected.get("no_fetch"):
        checks["no_unnecessary_fetch"] = not any(name.startswith("get_") for name in names)
    if expected.get("reuse_financials"):
        checks["reuse_financials"] = "get_financials" not in names and "get_financials" in conversation.research.get("reused_tools", [])
    if expected.get("fetch_financials"):
        checks["fetch_financials"] = "get_financials" in names
    finance = conversation.research.get("financials", {})
    if "finance_status" in expected:
        checks["finance_status"] = finance.get("status") == expected["finance_status"]
    if "finance" in expected:
        years, quarters = expected["finance"]
        checks["finance_scope"] = finance.get("requested") == {"annual_years": years, "quarters": quarters}
        checks["finance_rows"] = all(len(finance.get("statements", {}).get(freq, {}).get(name, [])) == count
            for freq, count in [("yearly", years), ("quarterly", quarters)] for name in ("income", "balance", "cashflow"))
    checks["no_tool_errors"] = all("error" not in call["result"] for call in conversation.tool_calls)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="실제 OpenAI API 호출 (유료)")
    parser.add_argument("--case", help="한 시나리오 ID만 실행")
    parser.add_argument("--report", type=Path, help="저장된 결과의 실패 사유 표시 (API 호출 없음)")
    args = parser.parse_args()
    if args.report:
        if args.live or args.case:
            parser.error("--report는 --live/--case와 함께 사용할 수 없습니다.")
        saved = json.loads(args.report.read_text(encoding="utf-8"))
        for row in saved["results"]:
            print_result(row)
        print("저장된 평가 결과입니다. 모델을 호출하거나 기존 점수를 변경하지 않았습니다.")
        return
    cases = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
    if not cases:
        parser.error("일치하는 평가 시나리오가 없습니다.")
    if len({case["id"] for case in cases}) != len(cases):
        parser.error("중복 시나리오 ID입니다.")
    for case in cases:
        if not case["turns"] or case.get("fixture", "complete") not in {"complete", "partial", "unavailable"}:
            parser.error("시나리오 설정을 확인하세요.")
        for turn in case["turns"]:
            if not isinstance(turn.get("input"), str) or not turn["input"].strip() or "code" not in turn or turn.get("horizon") not in {None, "단기", "중장기", "장기"}:
                parser.error("질문 또는 기대 상태 형식이 잘못되었습니다.")
    print(f"Validated {len(cases)} scenarios / {sum(len(c['turns']) for c in cases)} turns.")
    if not args.live:
        print("Dry run only. Use --live to call OpenAI; API charges apply.")
        return
    key, model = settings()
    if not key or key == "aaa":
        parser.error("유효한 OPENAI_API_KEY 설정이 필요합니다.")
    report = {"model": model, "prompt_sha256": hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
              "fixture_version": 1, "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
              "started_at": datetime.now(timezone.utc).isoformat(), "results": [],
              "manual_review": ["답변 숫자가 제공 자료와 일치하는가", "partial/unavailable 한계를 밝혔는가",
                                "결론을 연결된 근거가 실제로 뒷받침하는가" ]}
    for case in cases:
        conversation = OpenAIConversation()
        with fixed_data(case.get("fixture", "complete")):
            for index, turn in enumerate(case["turns"]):
                started = perf_counter()
                row = {"case": case["id"], "turn": index+1, "input": turn["input"]}
                try:
                    row["answer"] = conversation.respond(turn["input"])
                    row["checks"] = grade(turn, conversation)
                    row["passed"] = all(row["checks"].values())
                    row.update(tools=conversation.tool_calls, steps=conversation.agent_steps,
                               research=conversation.research, coverage=conversation.coverage)
                except ChatError:
                    # 원본 공급자 오류나 비밀 값을 결과 파일에 저장하지 않는다.
                    row.update(passed=False, error="ChatError: API 또는 실행 제한 오류")
                row["seconds"] = round(perf_counter()-started, 3)
                report["results"].append(row)
                print_result(row)
                if "error" in row:
                    # 실패한 멀티턴을 계속하면 이전 맥락이 없어 후속 결과가 왜곡된다.
                    break
    report["passed"] = sum(row["passed"] for row in report["results"])
    report["executed"] = len(report["results"])
    report["planned"] = sum(len(case["turns"]) for case in cases)
    report["skipped"] = report["planned"] - report["executed"]
    destination = Path(__file__).parent / "results"
    destination.mkdir(exist_ok=True)
    path = destination / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['passed']}/{report['executed']} passed; {report['skipped']} skipped. Report: {path}")
    if report["passed"] != report["planned"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

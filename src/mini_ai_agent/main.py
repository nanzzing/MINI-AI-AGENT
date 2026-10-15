"""터미널용 진입점. 웹과 동일한 OpenAI 대화를 입력·출력으로 실행한다."""

from mini_ai_agent.llm import OpenAIConversation, ChatError


def main() -> None:
    """터미널 입력 한 줄을 동일한 대화 엔진으로 보낸다. 차트/체크리스트 UI는 없다.

    pip 설치 후 mini-ai-agent 명령 또는 python -m mini_ai_agent.main으로 실행한다.
    /exit는 이 루프에서 처리하고 /reset은 llm.py가 대화 상태를 지우도록 전달한다.
    """
    # 반복문 밖에서 한 번 생성해야 후속 질문에도 이전 대화가 유지된다.
    conversation = OpenAIConversation()
    print("MINI-AI-AGENT | 종목 조사 도우미")
    print("종료: /exit | 새 대화: /reset")
    while True:
        try:
            message = input("\n나: ")
        except (EOFError, KeyboardInterrupt):
            # 입력 종료나 Ctrl+C는 정상 종료로 처리한다.
            print("\n대화를 종료합니다.")
            break
        if message.strip() == "/exit":
            break
        try:
            print(f"\n도우미: {conversation.respond(message)}")
        except ChatError as error:
            # 키·연결 오류를 안내한 뒤 다음 입력을 받으므로 프로그램이 바로 종료되지 않는다.
            print(f"\n연결 안내: {error}")


if __name__ == "__main__":
    main()

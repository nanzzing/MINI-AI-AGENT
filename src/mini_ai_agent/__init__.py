"""MINI-AI-AGENT 코드 읽기 안내.

추천 순서:
1. server.py: 브라우저 요청을 받는 입구와 JSON 응답 구조.
2. llm.py: 모델 호출 → 도구 요청 → Python 실행 → 최종 답변의 반복.
3. conversation.py / prompts/research.txt: 조사 기준과 모델 행동 지침.
4. stocks.py / market_data.py: 종목 식별과 외부 자료 수집.
5. technicals.py: 모델에게 전달하는 기술적 지표 계산.
6. coverage.py: 모델이 선언한 답변 문단-자료 ID 연결의 검증.
7. static/app.js → market.js → indicators.js → drawings.js: 화면 갱신.

공부할 때 따라갈 예제:
- '삼전 어때?'에서 검색 결과가 어떻게 code/exchange로 바뀌는지 추적한다.
- '장기'를 이어 보내고 history와 state가 각각 무엇을 보관하는지 비교한다.
- '중기'로 바꿔 saved_resources 원본과 scoped_resources 공개 자료의 차이를 본다.
- 도구 결과의 call_id가 다음 responses.create 입력에 연결되는 부분을 찾는다.
- 답변의 source_ids가 coverage.py를 거쳐 체크리스트 상태로 바뀌는 과정을 본다.

테스트 읽기: tests/test_llm.py의 make_client는 실제 HTTP 대신 준비된 모델 응답을
돌려준다. 테스트 통과는 코드 분기 검증이며 실제 모델의 판단 정확도 평가와는 다르다.
프롬프트 파일은 주석 문서가 아니라 모델에게 실제 보내는 지시문이므로 학습 설명을
그 안에 무작정 추가하면 동작이 달라진다. 프롬프트 구성 설명은 llm.py에서 읽는다.

main.py는 같은 대화 엔진을 쓰는 터미널 입구다. 이 파일 자체는 서버를
실행하지 않으며, mini_ai_agent를 하나의 Python 패키지로 묶는 역할을 한다.
"""

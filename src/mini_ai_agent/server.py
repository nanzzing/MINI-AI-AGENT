"""브라우저와 파이썬을 연결하는 로컬 HTTP 서버.

GET은 화면·설정 상태를 전달하고, POST /api/chat은 입력을 llm.py로 넘긴다.
모델 답변과 조사 상태를 JSON으로 반환하면 프론트가 대화와 체크리스트를 갱신한다.
"""

import argparse
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from threading import Lock
from urllib.parse import urlparse, parse_qs

from mini_ai_agent.conversation import RESEARCH_FOCUS
from mini_ai_agent.llm import OpenAIConversation, ChatError, configuration_status
from mini_ai_agent.stocks import find_stocks
from mini_ai_agent.market_data import stock_identity, price_history, financials, policy

STATIC = Path(__file__).parent / "static"
# 쿠키의 임의 식별자로 사용자별 대화를 구분한다. 서버를 종료하면 이 메모리는 사라진다.
SESSIONS: dict[str, OpenAIConversation] = {}
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, data: dict, cookie: str | None = None) -> None:
        """공통 JSON 응답 처리. 한국어를 UTF-8로 보내고 대화 응답 캐시를 막는다."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            # 세션 쿠키를 JS에서 직접 읽지 못하게 하고 다른 사이트 요청에서의 전송을 제한한다.
            self.send_header("Set-Cookie", f"session={cookie}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/api/market", "/api/financials"}:
            params = parse_qs(parsed.query)
            try:
                horizon = params.get("horizon", [None])[0]
                if horizon not in {None, "단기", "중기", "중장기", "장기"}:
                    raise ValueError("투자 기간을 확인해 주세요.")
                if "query" in params:
                    matches = find_stocks(params["query"][0][:200])
                    if len(matches) != 1:
                        self.send_json(200, {"status": "ambiguous" if matches else "not_found", "matches": matches[:10]})
                        return
                    stock = matches[0]
                else:
                    stock = stock_identity(params.get("code", [""])[0], params.get("exchange", [""])[0])
                result = price_history(stock, horizon) if parsed.path == "/api/market" else financials(stock, horizon)
                self.send_json(200, {"stock": stock, "data": result, "policy": policy(horizon)})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            return
        if self.path == "/api/status":
            self.send_json(200, configuration_status())
            return
        # 공개할 파일만 명시한다. .env나 서버 소스는 이 경로로 제공하지 않는다.
        files = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
                 "/app.js": ("app.js", "text/javascript"), "/lucide.js": ("lucide.js", "text/javascript"),
                 "/charts.js": ("charts.js", "text/javascript"), "/market.js": ("market.js", "text/javascript"),
                 "/indicators.js": ("indicators.js", "text/javascript"),
                 "/drawings.js": ("drawings.js", "text/javascript"),
                 "/market.css": ("market.css", "text/css")}
        target = files.get(self.path.split("?", 1)[0])
        if not target or not (STATIC / target[0]).is_file():
            self.send_error(404)
            return
        body = (STATIC / target[0]).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{target[1]}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """입력 검사 → 세션 조회 → 모델 대화 → 화면에 필요한 결과 반환."""
        if self.path != "/api/chat":
            self.send_json(404, {"error": "요청을 찾을 수 없어요."})
            return
        try:
            # 요청 크기와 메시지 형식을 먼저 확인해 잘못된 입력이 모델까지 가지 않도록 한다.
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            message = data.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise ValueError
        except (ValueError, AttributeError, UnicodeDecodeError):
            self.send_json(400, {"error": "메시지를 1~2,000자로 입력해 주세요."})
            return
        # 이전 요청의 쿠키가 있으면 동일한 대화 객체를 다시 사용한다.
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        session = cookies.get("session")
        token = session.value if session else ""
        with LOCK:
            # 전체 세션 저장소는 생성·조회할 때만 잠근다. 느린 API 호출 중에는 잠그지 않는다.
            if token not in SESSIONS:
                token = secrets.token_urlsafe(24)
                if len(SESSIONS) >= 1000:
                    # 메모리 사용을 제한하기 위해 가장 먼저 등록된 세션부터 제거한다.
                    SESSIONS.pop(next(iter(SESSIONS)))
                SESSIONS[token] = OpenAIConversation()
            conversation = SESSIONS[token]
        try:
            reply = conversation.respond(message)
            # 답변 외에 종목·기간·도구 실행 내역을 함께 보내 화면 상태도 맞춘다.
            state = conversation.state
            result = {"reply": reply, "company": state.company,
                      "code": state.code, "exchange": state.exchange,
                      "horizon": state.horizon.value if state.horizon else None,
                      "stage": state.stage,
                      "tool_calls": conversation.tool_calls,
                      "research": conversation.research,
                      "focus": list(RESEARCH_FOCUS[state.horizon]) if state.horizon else []}
        except ChatError as error:
            self.send_json(503, {"error": str(error)}, token)
            return
        self.send_json(200, result, token)


def main() -> None:
    """로컬은 루프백, 배포는 --host와 플랫폼의 PORT 환경변수를 사용한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"MINI-AI-AGENT: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

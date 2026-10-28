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
from mini_ai_agent.llm import OpenAIConversation, ChatError, configuration_status, api_model_options
from mini_ai_agent.stocks import find_stocks, search_stocks
from mini_ai_agent.discovery import discover
from mini_ai_agent.market_data import stock_identity, price_history, financials, policy
from mini_ai_agent.chatgpt import ChatGPTConnection, ChatGPTError
from mini_ai_agent.market_indicators import indicator
from mini_ai_agent.yahoo_diagnostics import start_startup_diagnostics

STATIC = Path(__file__).parent / "static"
# 쿠키의 임의 식별자로 사용자별 대화를 구분한다. 서버를 종료하면 이 메모리는 사라진다.
SESSIONS: dict[str, OpenAIConversation] = {}
# 브라우저 쿠키에는 식별자만 있고, 대화/보유 자료는 이 프로세스의 메모리에 있다.
# 여러 서버 프로세스 사이에서 공유되는 DB가 아니다. 재시작하면 세션 내용이 사라진다.
# LOCK은 세션 사전 접근용, 각 conversation.lock은 같은 사용자의 대화 순서 보장용이다.
LOCK = Lock()


class ClientDisconnected(Exception):
    """다음 단계 전송에서 연결 종료를 감지하면 추가 모델/도구 호출을 중단한다."""


class Handler(BaseHTTPRequestHandler):
    """요청 하나를 처리하는 객체. 영속 상태는 Handler가 아니라 SESSIONS에 둔다.

    GET /              : 화면 파일
    GET/HEAD /health   : 외부 공급자 호출 없는 서버 생존 확인
    GET /api/status    : 설정 유무와 관점별 체크리스트 (키 값은 제외)
    GET /api/market    : 차트용 전체 OHLCV 일봉
    GET /api/financials: 화면의 재무표 조회
    POST /api/chat     : 모델과 대화하고 답변·도구 내역·근거 연결 반환

    차트 GET은 모델을 호출하지 않는다. 따라서 차트를 조회했다는 사실만으로
    '모델 도구 요청' 횟수가 증가하지 않는다.
    """
    @property
    def chatgpt(self):
        return getattr(getattr(self, 'server', None), 'chatgpt', None)

    def local_request_allowed(self, write=False):
        """DNS rebinding/다른 웹사이트의 요청으로 로컬 구독을 쓰지 못하게 막는다."""
        if self.chatgpt is None:
            return True
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host = self.headers.get('Host', '')
        if any(self.headers.get(name) for name in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host', 'X-Real-IP')):
            return False
        if self.client_address[0] != '127.0.0.1' or host not in hosts:
            return False
        if write:
            return (self.headers.get('Origin') == 'http://' + host
                    and secrets.compare_digest(self.headers.get('X-Mini-CSRF', ''), self.server.csrf))
        return True

    def log_request(self, code='-', size='-'):
        # OAuth callback의 code/state와 검색 query를 로그에 남기지 않는다.
        self.log_message('"%s %s" %s %s', self.command, urlparse(self.path).path, str(code), str(size))

    def auth_post(self):
        if self.chatgpt is None:
            self.send_json(403, {'error': 'ChatGPT 구독 연결은 로컬 전용 모드에서만 사용할 수 있어요.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or any(not isinstance(value, (str, type(None))) for value in data.values()):
                raise ValueError
            if self.path == '/api/auth/login':
                url = self.chatgpt.begin_login(f'http://127.0.0.1:{self.server.server_port}/auth/callback', data.get('profile_id'))
                result = {'authorization_url': url}
            elif self.path == '/api/auth/models':
                result = self.chatgpt.refresh_models()
            elif self.path == '/api/auth/select':
                self.chatgpt.select(data.get('profile_id'), data.get('model'))
                with LOCK:
                    SESSIONS.clear()
                result = self.chatgpt.status()
            elif self.path == '/api/auth/logout':
                result = self.chatgpt.logout()
                with LOCK:
                    SESSIONS.clear()
            else:
                self.send_json(404, {'error': '요청을 찾을 수 없어요.'})
                return
            self.send_json(200, result)
        except ChatGPTError as error:
            self.send_json(400, {'error': str(error)})
        except (ValueError, OSError, TimeoutError):
            self.send_json(400, {'error': '인증 요청 또는 로컬 인증 저장소를 확인해 주세요.'})

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
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(body)

    def start_chat_stream(self, cookie):
        """진행 이벤트를 JSON 한 줄씩 보낸다. 기존 JSON 클라이언트는 그대로 지원한다."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, no-transform')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.send_header('Set-Cookie', f'session={cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.close_connection = True

    def send_chat_event(self, event):
        # 토큰 조각/비공개 추론이 아니라 서버가 관찰한 단계와 최종 결과만 공개한다.
        try:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8'))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError) as error:
            raise ClientDisconnected from error

    def finish_chat(self, status, data, token, streaming):
        if not streaming:
            self.send_json(status, data, token)
            return
        # 이미 HTTP 200 헤더를 보낸 스트림의 실패는 error 이벤트로 표현한다.
        try:
            self.send_chat_event({'type': 'result', 'data': data} if status == 200
                                 else {'type': 'error', 'error': data['error']})
        except ClientDisconnected:
            pass

    def do_HEAD(self) -> None:
        # health만 지원한다. 다른 GET API를 실행해 불필요한 외부 조회를 만들지 않는다.
        if urlparse(self.path).path == '/health':
            self.do_GET()
        else:
            self.send_error(404)

    def do_GET(self) -> None:
        # URL의 경로와 query string을 분리한다. parse_qs 결과의 값은 항상 리스트다.
        # query 검색은 후보가 하나일 때만 확정하고, code 조회는 거래소까지 검증한다.
        parsed = urlparse(self.path)
        if not self.local_request_allowed():
            self.send_json(403, {'error': '이 로컬 서버의 주소에서 직접 접속해 주세요.'})
            return
        if parsed.path == '/health':
            # 모델 인증/시장 데이터 상태가 아닌 HTTP 서버의 응답 가능 여부만 확인한다.
            self.send_json(200, {'status': 'ok'})
            return
        if parsed.path == '/auth/callback':
            if self.chatgpt is None:
                self.send_error(404)
                return
            try:
                params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
                if any(len(values) != 1 for values in params.values()):
                    raise ChatGPTError('로그인 응답 형식이 올바르지 않아요.')
                self.chatgpt.complete_login({key: value[0] for key, value in params.items()})
                with LOCK:
                    SESSIONS.clear()
                message = 'ChatGPT 계정을 연결했어요. 이 창을 닫고 MINI로 돌아가 주세요.'
            except ChatGPTError as error:
                message = str(error)
            except (ValueError, OSError, TimeoutError):
                message = '로컬 인증 정보를 저장하지 못했어요. MINI에서 다시 연결해 주세요.'
            from html import escape
            body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                    '<title>MINI ChatGPT 로그인</title><h1>MINI</h1><p>' + escape(message)
                    + '</p><a href="/">MINI로 돌아가기</a>').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/discovery":
            self.send_json(200, discover())
            return
        if parsed.path == '/api/market-indicator':
            name = parse_qs(parsed.query).get('name', [''])[0]
            try:
                self.send_json(200, indicator(name))
            except ValueError:
                self.send_json(400, {'error': '지원하지 않는 시장 지표입니다.'})
            return
        if parsed.path in {"/api/market", "/api/financials"}:
            params = parse_qs(parsed.query)
            try:
                horizon = params.get("horizon", [None])[0]
                if horizon not in {None, "단기", "중기", "중장기", "장기"}:
                    raise ValueError("투자 기간을 확인해 주세요.")
                if "query" in params:
                    search = search_stocks(params["query"][0][:200])
                    matches = search['stocks']
                    if len(matches) != 1:
                        self.send_json(200, {"status": "ambiguous" if matches else search['status'],
                                             "message": search.get('message'), "matches": matches[:10]})
                        return
                    stock = matches[0]
                else:
                    stock = stock_identity(params.get("code", [""])[0], params.get("exchange", [""])[0])
                # 차트의 전체 이력과 모델/재무 분석에 쓰는 관점별 범위를 분리한다.
                result = price_history(stock, full_history=True) if parsed.path == "/api/market" else financials(stock, horizon)
                self.send_json(200, {"stock": stock, "data": result, "policy": policy(horizon)})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            return
        if self.path == "/api/status":
            # 프론트가 체크리스트를 즉시 전환하도록 Python의 기준을 그대로 공유한다.
            # configured=True는 문자열 설정 존재 여부이지 OpenAI 인증 성공 여부가 아니다.
            try:
                status = self.chatgpt.status() if self.chatgpt is not None else {**configuration_status(), 'provider': 'api'}
                if self.chatgpt is None:
                    cookies = SimpleCookie()
                    cookies.load(self.headers.get('Cookie', ''))
                    cookie = cookies.get('session')
                    with LOCK:
                        conversation = SESSIONS.get(cookie.value) if cookie else None
                    selected = getattr(conversation, 'api_model', None)
                    if selected in {row['id'] for row in status['api_models']}:
                        status['model'] = selected
                self.send_json(200, {**status, "research_focus": RESEARCH_FOCUS,
                                     'csrf': self.server.csrf if self.chatgpt is not None else None})
            except (ValueError, OSError, TimeoutError):
                self.send_json(503, {'error': '로컬 인증 저장소를 열지 못했어요.'})
            return
        # 공개할 파일만 명시한다. .env나 서버 소스는 이 경로로 제공하지 않는다.
        files = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
                 "/app.js": ("app.js", "text/javascript"), "/lucide.js": ("lucide.js", "text/javascript"),
                 "/auth.js": ("auth.js", "text/javascript"),
                 "/market-indicators.js": ("market-indicators.js", "text/javascript"),
                 "/charts.js": ("charts.js", "text/javascript"), "/market.js": ("market.js", "text/javascript"),
                 "/indicators.js": ("indicators.js", "text/javascript"),
                 "/drawings.js": ("drawings.js", "text/javascript"),
                 "/market.css": ("market.css", "text/css"),
                 "/og-penguin.png": ("og-penguin.png", "image/png")}
        target = files.get(self.path.split("?", 1)[0])
        if not target or not (STATIC / target[0]).is_file():
            self.send_error(404)
            return
        body = (STATIC / target[0]).read_bytes()
        # PNG도 같은 경로로 보내므로 bytes로 읽는다. charset은 텍스트에만 붙인다.
        self.send_response(200)
        content_type = target[1]
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """입력 검사 → 세션 조회 → 모델 대화 → 화면에 필요한 결과 반환."""
        if not self.local_request_allowed(write=True):
            self.send_json(403, {'error': '로컬 화면을 새로고침한 뒤 다시 시도해 주세요.'})
            return
        if self.path.startswith('/api/auth/'):
            self.auth_post()
            return
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
            api_model = data.get('api_model')
            if api_model is not None and (self.chatgpt is not None or not isinstance(api_model, str) or api_model not in {row['id'] for row in api_model_options()[1]}):
                self.send_json(400, {'error': '허용되지 않은 API 모델입니다. 모델 목록을 새로고침해 주세요.'})
                return
            selection = data.get('selection', '자동')
            if selection not in ('자동', '단기', '중기', '장기'):
                raise ValueError
            if not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise ValueError
        except (ValueError, AttributeError, UnicodeDecodeError):
            self.send_json(400, {"error": "메시지를 1~2,000자로 입력해 주세요."})
            return
        # 이전 요청의 쿠키가 있으면 동일한 대화 객체를 다시 사용한다.
        cookies = SimpleCookie()
        # 새로고침은 쿠키를 지우지 않는다. 화면이 비어도 같은 서버 대화가 이어질 수 있다.
        # 세션은 프로세스 메모리이므로 서버 재시작 때 사라지고 여러 서버 간 공유되지 않는다.
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
                SESSIONS[token] = OpenAIConversation(chatgpt=self.chatgpt)
            conversation = SESSIONS[token]
        streaming = 'application/x-ndjson' in self.headers.get('Accept', '').split(',')
        try:
            model_args = {'api_model': api_model} if api_model is not None else {}
            if streaming:
                self.start_chat_stream(token)
                reply = conversation.respond(message, selection=selection, **model_args, on_progress=lambda step:
                                             self.send_chat_event({'type': 'progress', 'step': step}))
            else:
                reply = conversation.respond(message, selection=selection, **model_args)
            # respond가 성공한 상태만 읽는다. 문자열 답변과 별도로 구조화된 자료를 보내야
            # 브라우저가 체크리스트, 재무표, 도구 내역을 텍스트에서 재추출하지 않아도 된다.
            # 답변 외에 종목·기간·도구 실행 내역을 함께 보내 화면 상태도 맞춘다.
            state = conversation.state
            result = {"reply": reply, "company": state.company,
                      "code": state.code, "exchange": state.exchange,
                      "horizon": state.horizon.value if state.horizon else None,
                      "stage": state.stage,
                      "tool_calls": conversation.tool_calls,
                      "agent_steps": conversation.agent_steps,
                      # 실행 순서와 실제 도구 인자/결과를 별도 필드로 전송한다.
                      # 프론트는 이를 표시할 뿐 새 모델 호출이나 도구 실행을 하지 않는다.
                      "research": conversation.research,
                      "focus": list(RESEARCH_FOCUS[state.horizon]) if state.horizon else []}
            result["coverage"] = conversation.coverage
            if self.chatgpt is None:
                result['model'] = getattr(conversation, 'api_model', None)
            if conversation.research.get('response_mode', 'research') != 'research':
                result['focus'] = [row['criterion'] for row in conversation.coverage]
                result['horizon'] = None
            # coverage는 답변 생성 모델이 함께 반환한 근거 선언이다.
            # 여기서 별도 평가 모델을 호출하거나 투자 사실성을 검증하지 않는다.
        except ClientDisconnected:
            return
        except ChatError as error:
            self.finish_chat(503, {"error": str(error)}, token, streaming)
            return
        except Exception:
            # 예외 원문에는 외부 응답/자격 증명이 섞일 수 있어 브라우저에 보내지 않는다.
            self.finish_chat(500, {"error": "조사 처리 중 오류가 발생했어요. 다시 시도해 주세요."}, token, streaming)
            return
        self.finish_chat(200, result, token, streaming)


def main() -> None:
    """로컬은 루프백, 배포는 --host와 플랫폼의 PORT 환경변수를 사용한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument('--auth', choices=('auto', 'chatgpt', 'api'), default='auto')
    args = parser.parse_args()
    # 공개 바인딩/Render에서는 이 PC의 구독 자격 증명을 읽지 않는다.
    local = args.host == '127.0.0.1' and not os.environ.get('RENDER')
    if args.auth == 'chatgpt' and not local:
        parser.error('ChatGPT 구독 모드는 127.0.0.1 로컬 실행에서만 지원합니다.')
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.chatgpt = ChatGPTConnection() if local and args.auth != 'api' else None
    server.csrf = secrets.token_urlsafe(32)
    # 여러 사용자의 요청을 스레드로 처리한다. 개별 대화의 동시 변경은 llm.py에서 잠근다.
    print(f"MINI-AI-AGENT: http://{args.host}:{args.port}", flush=True)
    start_startup_diagnostics()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if server.chatgpt is not None:
            server.chatgpt.close()


if __name__ == "__main__":
    main()

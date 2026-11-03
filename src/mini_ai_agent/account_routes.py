"""사이트 로그인과 조사 기록 API. 로컬 ChatGPT 로그인과 별개의 계정이다."""
from dataclasses import asdict
from http.cookies import SimpleCookie
import json
from urllib.parse import parse_qs, urlparse

from mini_ai_agent.accounts import AccountError
from mini_ai_agent.conversation import ConversationState, Horizon
from mini_ai_agent.llm import OpenAIConversation


class AccountRoutes:
    @property
    def accounts(self):
        return getattr(getattr(self, 'server', None), 'accounts', None)

    def account_token(self):
        cookies = SimpleCookie()
        cookies.load(self.headers.get('Cookie', ''))
        cookie = cookies.get('__Host-mini_account' if self.accounts.config['environment'] == 'production' else 'mini_account')
        return cookie.value if cookie else ''

    def account_cookie(self, token, clear=False):
        production = self.accounts.config['environment'] == 'production'
        name = '__Host-mini_account' if production else 'mini_account'
        return f'{name}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={0 if clear else 86400}' + ('; Secure' if production else '')

    def account_write_allowed(self):
        return (self.headers.get('Origin') == self.accounts.config['origin']
                and self.headers.get('X-Mini-Account') == '1'
                and self.headers.get('Content-Type', '').split(';')[0] == 'application/json')

    def account_get(self, parsed):
        try:
            if not self.accounts:
                self.send_json(200, {'enabled': False, 'user': None})
                return
            session = None
            if self.account_token():
                try:
                    session = self.accounts.session(self.account_token())
                except AccountError as error:
                    if error.status != 401:
                        raise
            if parsed.path == '/api/account':
                self.send_json(200, {'enabled': True, 'environment': self.accounts.config['environment'],
                                     'user': session.user if session else None})
                return
            if not session:
                raise AccountError('로그인해 주세요.', 401)
            params = parse_qs(parsed.query)
            offset = int(params.get('offset', ['0'])[0])
            if not 0 <= offset <= 100000:
                raise ValueError
            if parsed.path == '/api/threads':
                data = {'threads': self.accounts.list(session, offset)}
            elif parsed.path == '/api/thread':
                identifier = params.get('id', [''])[0]
                data = {'thread': self.accounts.thread(session, identifier),
                        'turns': self.accounts.turns(session, identifier, offset)}
            else:
                raise AccountError('요청을 찾을 수 없어요.', 404)
            self.send_json(200, data)
        except AccountError as error:
            self.send_json(error.status, {'error': str(error)})
        except (ValueError, KeyError):
            self.send_json(400, {'error': '요청 값을 확인해 주세요.'})

    def account_post(self):
        try:
            if not self.accounts:
                raise AccountError('Supabase 개발용 설정이 필요합니다.', 503)
            if not self.account_write_allowed():
                raise AccountError('사이트에서 직접 요청해 주세요.', 403)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
            action = self.path.rsplit('/', 1)[-1]
            if action in ('login', 'signup', 'recovery', 'password', 'confirm'):
                email, password = data.get('email', ''), data.get('password', '')
                if not isinstance(email, str) or not 3 < len(email) <= 254 or '@' not in email:
                    raise ValueError
                if action not in ('recovery', 'confirm') and (not isinstance(password, str) or not 8 <= len(password) <= 128):
                    raise AccountError('비밀번호는 8~128자로 입력해 주세요.')
                if action == 'login':
                    token, session = self.accounts.login(email.strip(), password)
                    # 로그인 때 세션을 교체해 이전 계정/세션과 섞이지 않게 한다.
                    self.accounts.logout(self.account_token())
                    self.account_set_cookie = self.account_cookie(token)
                    result = {'user': session.user}
                elif action in ('password', 'confirm'):
                    code = data.get('code', '')
                    if not isinstance(code, str) or not code.isdigit() or not 6 <= len(code) <= 10:
                        raise AccountError('메일의 재설정 코드를 입력해 주세요.')
                    result = self.accounts.password(email.strip(), code, password) if action == 'password' else self.accounts.confirm(email.strip(), code)
                else:
                    result = getattr(self.accounts, action)(email.strip(), password) if action == 'signup' else self.accounts.recovery(email.strip())
            elif action == 'logout':
                self.accounts.logout(self.account_token())
                self.account_set_cookie = self.account_cookie('', clear=True)
                result = {'user': None}
            else:
                session = self.accounts.session(self.account_token())
                if action == 'create':
                    result = {'thread': self.accounts.create(session)}
                elif action == 'delete':
                    self.accounts.delete(session, data.get('id', ''))
                    result = {'deleted': True}
                else:
                    raise AccountError('요청을 찾을 수 없어요.', 404)
            self.send_json(200, result)
        except AccountError as error:
            self.send_json(error.status, {'error': str(error)})
        except (ValueError, KeyError, TypeError):
            self.send_json(400, {'error': '이메일과 요청 값을 확인해 주세요.'})

    def restore_research(self, data):
        if not self.account_write_allowed():
            raise AccountError('사이트에서 직접 요청해 주세요.', 403)
        session = self.accounts.session(self.account_token())
        try:
            thread = self.accounts.thread(session, data.get('thread_id', ''))
        except (ValueError, TypeError, AttributeError):
            raise AccountError('새 조사를 시작하거나 저장된 조사를 선택해 주세요.')
        conversation = OpenAIConversation(chatgpt=self.chatgpt)
        context = thread.get('context') or {}
        state = context.get('state') or {}
        if state:
            conversation.state = ConversationState(**{**state, 'horizon': Horizon(state['horizon']) if state.get('horizon') else None})
        conversation.dialogue = context.get('dialogue', [])[-4:]
        # 과거 답변은 이어받되 외부 자료 캐시는 복원하지 않는다. 새 요청에서 재검증한다.
        return session, thread, conversation

    def persist_research(self, session, thread, conversation, message, result):
        context = {'state': asdict(conversation.state), 'dialogue': conversation.dialogue[-4:]}
        try:
            self.accounts.save(session, thread, message, result, context)
            result['saved'] = True
        except AccountError:
            result['saved'] = False
            result['storage_warning'] = '답변은 생성했지만 저장하지 못했어요. 다른 탭에서 변경했거나 DB 연결이 실패했습니다. 이 답변은 새로고침하면 사라질 수 있어요.'
        result['thread_id'] = thread['id']

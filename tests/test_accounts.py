"""실제 키/메일/모델 없이 환경 분리, 인증 경계, 저장 계약을 확인한다."""
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import httpx

from mini_ai_agent.accounts import Accounts, AccountError, LoginSession, configuration
from mini_ai_agent.server import Handler

CONFIG = {'environment': 'development', 'url': 'https://dev.supabase.co',
          'key': 'sb_publishable_test', 'origin': 'http://127.0.0.1:8765'}
THREAD = 'ba3cfc26-f124-4001-9aca-eefdc2688893'


class SettingsTests(unittest.TestCase):
    def test_environment_keys_do_not_fallback(self):
        self.assertIsNone(configuration({}))
        values = {'DEV_SUPABASE_URL': CONFIG['url'], 'DEV_SUPABASE_PUBLISHABLE_KEY': CONFIG['key']}
        self.assertEqual(configuration(values)['url'], CONFIG['url'])
        with self.assertRaises(ValueError):
            configuration({**values, 'RENDER': 'true'})
        with self.assertRaises(ValueError):
            configuration({**values, 'RENDER': 'true', 'APP_ENV': 'development'})
        with self.assertRaises(ValueError):
            configuration({**values, 'PROD_SUPABASE_URL': CONFIG['url']})

    def test_rejects_secret_keys_and_invalid_origin(self):
        values = {'APP_ENV': 'production', 'PROD_SUPABASE_URL': CONFIG['url'],
                  'PROD_SUPABASE_PUBLISHABLE_KEY': CONFIG['key'], 'APP_ORIGIN': 'https://mini.example'}
        self.assertEqual(configuration(values)['environment'], 'production')
        for override in ({'PROD_SUPABASE_PUBLISHABLE_KEY': 'sb_secret_private'}, {'APP_ORIGIN': 'http://mini.example'},
                         {'PROD_SUPABASE_URL': 'https://evil.example/dev.supabase.co'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                configuration({**values, **override})


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []
        def handle(request):
            self.requests.append(request)
            status, data = self.responses.pop(0)
            return httpx.Response(status, json=data)
        self.accounts = Accounts(CONFIG, httpx.Client(base_url=CONFIG['url'], transport=httpx.MockTransport(handle)))
        self.addCleanup(self.accounts.close)
        self.session = LoginSession({'id':'user-a','email':'a@example.test'}, 'user-access', 'user-refresh', time.time() + 3600)

    def test_login_tokens_stay_server_side_and_logout_invalidates(self):
        self.responses = [(200, {'user':self.session.user, 'access_token':'access', 'refresh_token':'refresh', 'expires_in':3600}),
                          (200, self.session.user), (200, {})]
        token, session = self.accounts.login('a@example.test', 'password123')
        self.assertNotIn(token, ('access', 'refresh'))
        self.assertEqual(self.accounts.session(token), session)
        self.accounts.logout(token)
        with self.assertRaises(AccountError):
            self.accounts.session(token)

    def test_refresh_rotation_and_expired_session(self):
        self.session.expires = 0
        self.accounts.sessions['session'] = self.session
        self.responses = [(200, {'access_token':'new', 'refresh_token':'new-refresh', 'expires_in':3600}), (200, self.session.user)]
        self.accounts.session('session')
        self.assertEqual(self.session.refresh, 'new-refresh')
        self.session.deadline = 0
        with self.assertRaises(AccountError):
            self.accounts.session('session')

    def test_database_uses_user_token_not_admin_and_not_found_is_404(self):
        self.responses = [(200, [])]
        with self.assertRaises(AccountError) as caught:
            self.accounts.thread(self.session, THREAD)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer user-access')
        self.assertEqual(self.requests[0].headers['apikey'], CONFIG['key'])

    def test_atomic_save_excludes_large_raw_data(self):
        self.responses = [(200, 1)]
        self.accounts.save(self.session, {'id':THREAD, 'revision':0}, '삼전 어때?',
                           {'reply':'답변', 'coverage':[], 'research':{'huge':'raw'}, 'tool_calls':[{'result':'raw'}]}, {'state':{}})
        request = self.requests[0]
        self.assertEqual(request.url.path, '/rest/v1/rpc/append_research_turn')
        payload = json.loads(request.content)
        self.assertEqual(payload['p_revision'], 0)
        self.assertNotIn('research', payload['p_result'])
        self.assertNotIn('tool_calls', payload['p_result'])

    def test_provider_errors_are_redacted_and_rate_limited(self):
        self.responses = [(400, {'secret':'sensitive'})]
        with self.assertRaises(AccountError) as caught:
            self.accounts.login('a@example.test', 'password123')
        self.assertNotIn('sensitive', str(caught.exception))
        for _ in range(9):
            self.accounts.limit_auth('a@example.test')
        with self.assertRaises(AccountError) as caught:
            self.accounts.limit_auth('a@example.test')
        self.assertEqual(caught.exception.status, 429)


class RouteTests(unittest.TestCase):
    def handler(self, path, data=None):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(accounts=MagicMock(config=CONFIG), chatgpt=None)
        handler.path = path
        body = json.dumps(data or {}).encode()
        handler.headers = {'Origin': CONFIG['origin'], 'X-Mini-Account':'1', 'Content-Type':'application/json', 'Content-Length':str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.send_json = MagicMock()
        return handler

    def test_csrf_and_cookie_flags(self):
        handler = self.handler('/api/account/login', {'email':'a@example.test', 'password':'password123'})
        handler.headers['Origin'] = 'https://evil.example'
        handler.account_post()
        self.assertEqual(handler.send_json.call_args.args[0], 403)
        handler.accounts.login.assert_not_called()
        handler.accounts.config = {**CONFIG, 'environment':'production'}
        cookie = handler.account_cookie('opaque')
        for flag in ('__Host-mini_account=', 'HttpOnly', 'Secure', 'SameSite=Strict'):
            self.assertIn(flag, cookie)

    def test_anonymous_chat_never_calls_model(self):
        handler = self.handler('/api/chat', {'message':'삼전 장기', 'thread_id':THREAD})
        handler.accounts.session.side_effect = AccountError('로그인 필요', 401)
        with patch('mini_ai_agent.server.OpenAIConversation') as model:
            handler.do_POST()
        model.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 401)

    def test_restore_does_not_restore_stale_market_resources(self):
        handler = self.handler('/api/chat')
        handler.accounts.thread.return_value = {'id':THREAD, 'revision':3, 'context': {
            'state':{'company':'삼성전자','code':'005930','exchange':'KOSPI','horizon':'장기','stage':'ready'},
            'dialogue':[{'question':'어때?','answer':'과거 답변'}], 'saved_resources':{'old':'prices'}}}
        _, _, conversation = handler.restore_research({'thread_id':THREAD})
        self.assertEqual(conversation.state.code, '005930')
        self.assertEqual(conversation.state.horizon.value, '장기')
        self.assertEqual(conversation.dialogue[0]['answer'], '과거 답변')
        self.assertEqual(conversation.saved_resources, {})

    def test_storage_failure_preserves_answer_with_warning(self):
        handler = self.handler('/api/chat')
        handler.accounts.save.side_effect = AccountError('DB failure', 503)
        from mini_ai_agent.llm import OpenAIConversation
        result = {'reply':'already generated'}
        handler.persist_research(None, {'id':THREAD}, OpenAIConversation(), 'question', result)
        self.assertFalse(result['saved'])
        self.assertEqual(result['reply'], 'already generated')
        self.assertIn('storage_warning', result)

    def test_authenticated_chat_restores_and_saves_to_selected_thread(self):
        handler = self.handler('/api/chat', {'message':'그럼 단기는?', 'thread_id':THREAD})
        handler.accounts.thread.return_value = {'id':THREAD, 'revision':2, 'context':{}}
        from mini_ai_agent.llm import OpenAIConversation
        conversation = OpenAIConversation()
        conversation.respond = MagicMock(return_value='저장할 답변')
        with patch('mini_ai_agent.account_routes.OpenAIConversation', return_value=conversation):
            handler.do_POST()
        status, result, _ = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(result['saved'])
        self.assertEqual(result['thread_id'], THREAD)
        self.assertEqual(result['reply'], '저장할 답변')
        saved = handler.accounts.save.call_args.args
        self.assertEqual(saved[1]['revision'], 2)
        self.assertEqual(saved[2], '그럼 단기는?')


if __name__ == '__main__':
    unittest.main()

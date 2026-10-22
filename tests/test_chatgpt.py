"""실제 계정/사용량 없이 공식 OAuth, 보호 저장, SSE, 로컬 요청 경계를 검사한다."""
import base64
import hashlib
import json
import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
from openai import OpenAI

from mini_ai_agent.auth_storage import CredentialStore
from mini_ai_agent.chatgpt import (ChatGPTConnection, ChatGPTError, SubscriptionClient,
                                   _validate_identity, ISSUER, RESOURCE, TOKEN_URL)
from mini_ai_agent.llm import OpenAIConversation, ChatError
from mini_ai_agent.server import Handler


class SignInTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = CredentialStore(folder.name)
        self.requests = []
        self.payload = {'access_token': 'test-access-secret', 'refresh_token': 'test-refresh-secret',
                        'id_token': 'test-id-secret', 'token_type': 'Bearer', 'expires_in': 3600,
                        'scope': 'openid resource.invoke chatgpt.tokens.use.direct'}
        self.token_status = 200
        self.revoke_status = 200
        self.models = [{'visibility': 'list', 'slug': 'allowed-model', 'display_name': 'Allowed model'},
                       {'visibility': 'hide', 'slug': 'hidden-model'}]

        def handle(request):
            self.requests.append(request)
            if str(request.url) == TOKEN_URL:
                return httpx.Response(self.token_status, json=self.payload)
            if str(request.url) == RESOURCE + '/models':
                return httpx.Response(200, json={'models': self.models})
            if str(request.url) == ISSUER + '/.well-known/openid-configuration':
                return httpx.Response(200, json={'revocation_endpoint': ISSUER + '/revoke'})
            if str(request.url) == ISSUER + '/revoke':
                return httpx.Response(self.revoke_status)
            raise AssertionError('Unexpected network destination')

        self.connection = ChatGPTConnection(self.store, httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(self.connection.close)
        self.identity = patch('mini_ai_agent.chatgpt._validate_identity', return_value={'sub': 'subject-a', 'email': 'test@example.com'})
        self.validate = self.identity.start()
        self.addCleanup(self.identity.stop)

    def login(self, profile_id=None):
        url = self.connection.begin_login('http://127.0.0.1:8765/auth/callback', profile_id)
        params = {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}
        issued = 'oaiapp_test-client' if params['client_id'] == 'dynamic_agent_client' else params['client_id']
        self.connection.complete_login({'state': params['state'], 'code': 'test-code', 'client_id': issued})
        return params

    def test_pkce_callback_token_exchange_and_public_status(self):
        url = self.connection.begin_login('http://127.0.0.1:8765/auth/callback')
        params = {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}
        attempt = self.connection.pending[params['state']]
        expected = base64.urlsafe_b64encode(hashlib.sha256(attempt['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertEqual(params['client_id'], 'dynamic_agent_client')
        self.assertEqual(params['code_challenge'], expected)
        self.assertEqual(params['agent_name_hint'], 'MINI-AI-AGENT')
        self.assertTrue(params['ext_agent_host_id'].startswith('urn:uuid:'))
        self.assertNotIn('client_secret', params)
        self.connection.complete_login({'state': params['state'], 'code': 'test-code', 'client_id': 'oaiapp_test-client'})
        form = parse_qs(self.requests[0].content.decode())
        self.assertEqual(form['client_id'], ['oaiapp_test-client'])
        self.assertEqual(form['redirect_uri'], [params['redirect_uri']])
        self.assertEqual(form['code_verifier'], [attempt['verifier']])
        self.assertEqual(form['resource'], [RESOURCE])
        self.validate.assert_called_with('test-id-secret', 'oaiapp_test-client', params['nonce'])
        status = self.connection.status()
        self.assertTrue(status['connected'])
        self.assertFalse(status['configured'])  # 아직 모델 목록을 확인하지 않았다.
        public = json.dumps(status)
        for secret in ('test-access-secret', 'test-refresh-secret', 'test-id-secret', 'subject-a'):
            self.assertNotIn(secret, public)
        if os.name == 'nt':
            self.assertNotIn(b'test-access-secret', self.store.path.read_bytes())
        else:
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_unknown_replayed_and_expired_states_do_not_exchange(self):
        with self.assertRaises(ChatGPTError):
            self.connection.complete_login({'state': 'wrong', 'code': 'x'})
        params = self.login()
        before = len(self.requests)
        with self.assertRaises(ChatGPTError):
            self.connection.complete_login({'state': params['state'], 'code': 'again'})
        self.assertEqual(len(self.requests), before)
        url = self.connection.begin_login('http://127.0.0.1:8765/auth/callback')
        state = parse_qs(urlparse(url).query)['state'][0]
        self.connection.pending[state]['expires'] = 0
        with self.assertRaises(ChatGPTError):
            self.connection.complete_login({'state': state, 'code': 'expired'})
        self.assertEqual(len(self.requests), before)

    def test_cancel_or_missing_client_does_not_activate(self):
        for extra in ({'error': 'access_denied'}, {'code': 'test-code'}):
            url = self.connection.begin_login('http://127.0.0.1:8765/auth/callback')
            state = parse_qs(urlparse(url).query)['state'][0]
            with self.assertRaises(ChatGPTError):
                self.connection.complete_login({'state': state, **extra})
            self.assertIsNone(self.connection.status()['active_profile'])
        self.assertFalse(self.requests)

    def test_models_are_discovered_and_model_selection_is_validated(self):
        self.login()
        status = self.connection.refresh_models()
        self.assertEqual(status['model'], 'allowed-model')
        self.assertTrue(status['configured'])
        self.assertEqual(len(status['models']), 1)
        self.assertEqual(self.requests[-1].headers['Authorization'], 'Bearer test-access-secret')
        with self.assertRaises(ChatGPTError):
            self.connection.select(model='arbitrary-model')
        with self.assertRaises(ChatGPTError):
            self.connection.select(profile_id='missing')

    def test_declined_plan_permission_cannot_make_requests(self):
        self.payload['scope'] = 'openid email'
        self.login()
        self.assertTrue(self.connection.status()['connected'])
        self.assertFalse(self.connection.status()['plan_enabled'])
        with self.assertRaises(ChatGPTError):
            self.connection.refresh_models()
        with self.assertRaises(ChatGPTError):
            self.connection.client()

    def test_registration_and_host_survive_logout_and_code_exchange_failure(self):
        first = self.login()
        profile = self.connection.status()['active_profile']
        self.connection.logout()
        self.assertFalse(self.connection.status()['connected'])
        second = self.login(profile)
        self.assertEqual(second['client_id'], 'oaiapp_test-client')
        self.assertEqual(first['ext_agent_host_id'], second['ext_agent_host_id'])
        self.assertNotIn('agent_name_hint', second)
        self.assertNotIn('id_token_hint', second)  # 토큰을 프론트에 보내지 않는다.
        self.connection.logout()
        self.token_status, self.payload = 400, {'error': 'invalid_grant'}
        with self.assertRaises(ChatGPTError):
            self.login(profile)
        self.assertEqual(len(self.connection.status()['profiles']), 1)

    def test_returning_login_rejects_client_or_identity_mismatch(self):
        self.login()
        profile = self.connection.status()['active_profile']
        url = self.connection.begin_login('http://127.0.0.1:8765/auth/callback', profile)
        state = parse_qs(urlparse(url).query)['state'][0]
        before = len(self.requests)
        with self.assertRaises(ChatGPTError):
            self.connection.complete_login({'state': state, 'code': 'x', 'client_id': 'different'})
        self.assertEqual(len(self.requests), before)
        self.validate.return_value = {'sub': 'other-subject', 'email': 'other@example.com'}
        with self.assertRaises(ChatGPTError):
            self.login(profile)
        self.assertEqual(self.connection.status()['profiles'][0]['label'], 'test@example.com')

    def test_refresh_rotation_and_terminal_failure(self):
        self.login()
        profile = self.connection.status()['active_profile']
        with self.store.locked() as data:
            data['profiles'][profile]['expires_at'] = 0
        self.payload = {'access_token': 'new-access', 'refresh_token': 'new-refresh',
                        'token_type': 'Bearer', 'expires_in': 3600}
        self.assertEqual(self.connection._access_token(profile), 'new-access')
        form = parse_qs(self.requests[-1].content.decode())
        self.assertEqual(form['grant_type'], ['refresh_token'])
        self.assertNotIn('scope', form)
        with self.store.locked() as data:
            self.assertEqual(data['profiles'][profile]['refresh_token'], 'new-refresh')
            data['profiles'][profile]['expires_at'] = 0
        self.token_status, self.payload = 400, {'error': 'invalid_grant'}
        with self.assertRaises(ChatGPTError):
            self.connection._access_token(profile)
        self.assertFalse(self.connection.status()['connected'])

    def test_temporary_refresh_failure_keeps_credentials(self):
        self.login()
        profile = self.connection.status()['active_profile']
        with self.store.locked() as data:
            data['profiles'][profile]['expires_at'] = 0
        self.token_status, self.payload = 503, {'error': 'temporarily_unavailable'}
        with self.assertRaises(ChatGPTError):
            self.connection._access_token(profile)
        self.assertTrue(self.connection.status()['connected'])

    def test_failed_revocation_clears_local_tokens_and_reports_limitation(self):
        self.login()
        self.revoke_status = 503
        with patch('mini_ai_agent.chatgpt.time.sleep'):
            result = self.connection.logout()
        self.assertIn('원격', result['message'])
        self.assertFalse(self.connection.status()['connected'])
        self.assertEqual(sum(str(r.url).endswith('/revoke') for r in self.requests), 2)

    def test_api_key_is_never_fallback_for_subscription_login_failure(self):
        with patch('mini_ai_agent.llm.settings') as settings, patch('mini_ai_agent.llm.OpenAI') as api:
            with self.assertRaises(ChatError):
                OpenAIConversation(chatgpt=self.connection).respond('안녕')
            settings.assert_not_called()
            api.assert_not_called()


class IdentityTests(unittest.TestCase):
    def test_validates_signature_audience_issuer_expiration_and_nonce(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'sub': 'user', 'iss': ISSUER, 'aud': 'issued-client',
                  'iat': int(time.time()), 'exp': int(time.time()) + 600, 'nonce': 'expected'}
        with patch('mini_ai_agent.chatgpt.jwt.PyJWKClient') as keys:
            keys.return_value.get_signing_key_from_jwt.return_value.key = private.public_key()
            encoded = jwt.encode(claims, private, algorithm='RS256')
            self.assertEqual(_validate_identity(encoded, 'issued-client', 'expected')['sub'], 'user')
            for changes in ({'iss': 'https://evil.example'}, {'aud': 'wrong'}, {'exp': 1}, {'nonce': 'wrong'}):
                with self.subTest(changes=changes), self.assertRaises(ChatGPTError):
                    _validate_identity(jwt.encode({**claims, **changes}, private, algorithm='RS256'), 'issued-client', 'expected')
            with self.assertRaises(ChatGPTError):
                _validate_identity(jwt.encode(claims, 'wrong-secret' * 4, algorithm='HS256'), 'issued-client', 'expected')


class TransportTests(unittest.TestCase):
    def run_stream(self, events, tools=None, choice=None):
        self.sent = []
        def handle(request):
            self.sent.append(json.loads(request.content))
            data = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
            return httpx.Response(200, text=data, headers={'Content-Type': 'text/event-stream'})
        real = OpenAI(api_key='test-only', http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(real.close)
        connection = MagicMock()
        connection._access_token.return_value = 'test-only'
        client = SubscriptionClient(connection, 'p1', 'revision')
        args = {'model': 'allowed-model', 'input': [{'role': 'user', 'content': 'hello'}],
                'instructions': 'test', 'tools': tools or [], 'max_output_tokens': 4000,
                'store': False, 'text': {'format': {'type': 'json_object'}}}
        if choice:
            args['tool_choice'] = choice
        with patch('mini_ai_agent.chatgpt.OpenAI', return_value=real):
            return client.create(**args)

    def completed(self, output=None):
        return {'type': 'response.completed', 'sequence_number': 1, 'response': {
            'id': 'resp_test', 'object': 'response', 'created_at': 0,
            'model': 'allowed-model', 'status': 'completed', 'output': output or []}}

    def test_namespaced_tools_and_required_streaming_contract(self):
        output = [{'type': 'function_call', 'id': 'fc1', 'call_id': 'c1', 'namespace': 'research',
                   'name': 'search_stock', 'arguments': '{"query":"삼전"}', 'status': 'completed'}]
        tool = {'type': 'function', 'name': 'search_stock', 'parameters': {}, 'strict': True}
        response = self.run_stream([self.completed(output)], [tool])
        self.assertEqual(response.output[0].name, 'search_stock')
        sent = self.sent[0]
        self.assertTrue(sent['stream'])
        self.assertFalse(sent['store'])
        self.assertNotIn('max_output_tokens', sent)
        self.assertEqual(sent['tools'][0]['type'], 'namespace')
        self.assertEqual(sent['tools'][0]['tools'], [tool])
        self.assertEqual(sent['include'], ['reasoning.encrypted_content'])

    def test_forced_tool_limits_namespace_members(self):
        tools = [{'type': 'function', 'name': name} for name in ('search_stock', 'set_research_context')]
        self.run_stream([self.completed()], tools, {'type': 'function', 'name': 'set_research_context'})
        self.assertEqual(self.sent[0]['tool_choice'], 'required')
        self.assertEqual([t['name'] for t in self.sent[0]['tools'][0]['tools']], ['set_research_context'])

    def test_empty_terminal_output_uses_completed_stream_items(self):
        items = [
            {'type': 'reasoning', 'id': 'rs1', 'summary': [], 'encrypted_content': 'test-encrypted'},
            {'type': 'function_call', 'id': 'fc1', 'call_id': 'c1', 'namespace': 'research',
             'name': 'set_research_context', 'arguments': '{"code":"005930"}', 'status': 'completed'},
        ]
        events = [{'type': 'response.output_item.done', 'sequence_number': i,
                   'output_index': i, 'item': item} for i, item in enumerate(items)]
        response = self.run_stream(events + [self.completed()])
        self.assertEqual([item.type for item in response.output], ['reasoning', 'function_call'])
        self.assertEqual(response.output[1].arguments, '{"code":"005930"}')
        self.assertEqual(response.output[0].encrypted_content, 'test-encrypted')
        full = self.run_stream(events + [self.completed(items)])
        self.assertEqual(len(full.output), 2)
        with self.assertRaises(ChatGPTError):
            self.run_stream(events)

    def test_streamed_final_text_is_preserved(self):
        item = {'type': 'message', 'id': 'msg1', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': '{"sections":[]}', 'annotations': []}]}
        response = self.run_stream([{'type': 'response.output_item.done', 'sequence_number': 0,
                                    'output_index': 0, 'item': item}, self.completed()])
        self.assertEqual(response.output_text, '{"sections":[]}')

    def test_missing_completion_failed_and_incomplete_streams_are_errors(self):
        for events in ([], [{'type': 'response.incomplete', 'sequence_number': 1,
                             'response': self.completed()['response']}],
                       [{'type': 'error', 'sequence_number': 1, 'code': 'subscription_sharing_usage_limit_exceeded',
                         'message': 'secret-raw-detail', 'param': None}]):
            with self.subTest(events=events), self.assertRaises(ChatGPTError) as caught:
                self.run_stream(events)
            self.assertNotIn('secret-raw-detail', str(caught.exception))

    def test_existing_agent_loop_round_trips_through_subscription_without_api_key(self):
        requests = []
        def call(name, args, identifier):
            return {'type': 'function_call', 'id': identifier, 'call_id': identifier,
                    'namespace': 'research', 'name': name, 'arguments': json.dumps(args), 'status': 'completed'}
        outputs = [
            [call('search_stock', {'query': '삼전'}, 'c1')],
            [{'type': 'reasoning', 'id': 'rs_test', 'summary': [], 'encrypted_content': 'encrypted-test'},
             call('set_research_context', {'code': '005930', 'exchange': 'KOSPI', 'horizon': '단기'}, 'c2')],
            [call(name, {}, name) for name in ('get_price_analysis', 'get_financials', 'get_news', 'get_events')],
            [{'type': 'message', 'id': 'msg_test', 'role': 'assistant', 'status': 'completed',
              'content': [{'type': 'output_text', 'annotations': [],
                           'text': json.dumps({'sections': [{'text': '단기 조사 완료', 'source_ids': []}]})}]}],
        ]
        def handle(request):
            requests.append(json.loads(request.content))
            event = self.completed(outputs.pop(0))
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n',
                                  headers={'Content-Type': 'text/event-stream'})
        def sdk(**kwargs):
            self.assertEqual(kwargs['api_key'], 'test-subscription-only')
            return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        connection = MagicMock()
        connection._access_token.return_value = 'test-subscription-only'
        connection.client.return_value = (SubscriptionClient(connection, 'profile', 'revision'), 'allowed-model')
        with patch('mini_ai_agent.chatgpt.OpenAI', side_effect=sdk), \
             patch('mini_ai_agent.llm.OpenAI') as api, patch('mini_ai_agent.llm.settings') as settings, \
             patch('mini_ai_agent.llm.price_history', return_value={'status': 'unavailable', 'bars': []}), \
             patch('mini_ai_agent.llm.financials', return_value={'status': 'unavailable', 'statements': {}}), \
             patch('mini_ai_agent.llm.news', return_value={'status': 'ok', 'items': []}), \
             patch('mini_ai_agent.llm.earnings_events', return_value={'status': 'ok', 'items': []}):
            conversation = OpenAIConversation(chatgpt=connection)
            self.assertEqual(conversation.respond('삼전 단기'), '단기 조사 완료')
            self.assertEqual(conversation.state.code, '005930')
            self.assertEqual(len(conversation.tool_calls), 6)
            self.assertEqual(len(requests), 4)
            tool_results = [item for item in requests[-1]['input'] if item.get('type') == 'function_call_output']
            # 상태 변경 시 과거 검색 결과는 제거하지만, 현재 함수 호출과 추론 항목은 보존한다.
            self.assertEqual(len(tool_results), 5)
            self.assertTrue(any(item.get('call_id') == 'c1' for item in requests[1]['input']))
            self.assertTrue(any(item.get('encrypted_content') == 'encrypted-test' for item in requests[-1]['input']))
            self.assertTrue(all(request['stream'] for request in requests))
            api.assert_not_called()
            settings.assert_not_called()

    def test_foreign_namespace_is_rejected(self):
        output = [{'type': 'function_call', 'id': 'fc1', 'call_id': 'c1', 'namespace': 'other',
                   'name': 'search_stock', 'arguments': '{}', 'status': 'completed'}]
        with self.assertRaises(ChatGPTError):
            self.run_stream([self.completed(output)])


class LocalBoundaryTests(unittest.TestCase):
    def handler(self, **headers):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(chatgpt=object(), server_port=8765, csrf='known-csrf')
        handler.client_address = ('127.0.0.1', 50000)
        handler.headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
                           'X-Mini-CSRF': 'known-csrf', **headers}
        return handler

    def test_local_write_requires_host_origin_csrf_and_no_proxy(self):
        self.assertTrue(self.handler().local_request_allowed(write=True))
        for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                        {'Origin': ''}, {'X-Mini-CSRF': ''}, {'X-Forwarded-For': '10.0.0.1'}):
            with self.subTest(headers=headers):
                self.assertFalse(self.handler(**headers).local_request_allowed(write=True))

    def test_remote_auth_endpoints_are_disabled_and_callback_logs_are_redacted(self):
        handler = self.handler()
        handler.server.chatgpt = None
        handler.path = '/api/auth/login'
        handler.send_json = MagicMock()
        handler.auth_post()
        self.assertEqual(handler.send_json.call_args.args[0], 403)
        handler.path = '/auth/callback?code=SECRET&state=SECRET'
        handler.command = 'GET'
        handler.log_message = MagicMock()
        handler.log_request(200, 100)
        self.assertNotIn('SECRET', str(handler.log_message.call_args))


if __name__ == '__main__':
    unittest.main()

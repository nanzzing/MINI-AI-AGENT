"""API 모델 목록과 세션별 선택의 HTTP 계약. 실제 API를 호출하지 않는다."""
import http.client
import json
import unittest
from threading import Thread
from unittest.mock import patch
from mini_ai_agent import llm, server
from mini_ai_agent.conversation import ConversationState


class ApiModelTests(unittest.TestCase):
    def test_default_choices_include_gpt6_without_changing_default(self):
        with patch.object(llm, 'dotenv_values', return_value={}), patch.dict(llm.os.environ, {}, clear=True):
            default, rows = llm.api_model_options('gpt-4.1-mini')
        self.assertEqual(default, 'gpt-4.1-mini')
        labels = {row['id']: row['label'] for row in rows}
        for model, label in [('gpt-6-luna', 'GPT-6 Luna'), ('gpt-6-sol', 'GPT-6 Sol'),
                             ('gpt-6.1-sol', 'GPT-6.1 Sol'), ('gpt-6-astra', 'GPT-6 Astra')]:
            self.assertEqual(labels[model], label)

    def test_allowlist_overrides_and_safe_default(self):
        with patch.object(llm, 'dotenv_values', return_value={}), patch.dict(llm.os.environ, {'OPENAI_ALLOWED_MODELS': 'gpt-5.4-mini,gpt-5.4-mini,gpt-5.4'}):
            default, rows = llm.api_model_options('gpt-4.1-mini')
        self.assertEqual(default, 'gpt-5.4-mini')
        self.assertEqual([row['id'] for row in rows], ['gpt-5.4-mini', 'gpt-5.4'])

    def test_http_model_selection_is_isolated_and_invalid_is_400(self):
        class FakeConversation:
            def __init__(self, **kwargs):
                self.state = ConversationState()
                self.api_model = None
                self.tool_calls, self.agent_steps, self.coverage = [], [], []
                self.research = {}
            def respond(self, message, **kwargs):
                self.api_model = kwargs.get('api_model')
                return 'ok'
        choices = ('gpt-4.1-mini', [{'id': 'gpt-4.1-mini', 'label': 'mini'}, {'id': 'gpt-5.4', 'label': 'full'}])
        with patch.object(server, 'OpenAIConversation', FakeConversation), patch.object(server, 'api_model_options', return_value=choices), patch.object(llm, 'api_model_options', return_value=choices), patch.object(llm, 'settings', return_value=('secret-test', 'gpt-4.1-mini')), patch.dict(server.SESSIONS, {}, clear=True), patch.object(server.Handler, 'log_message'):
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            Thread(target=httpd.serve_forever, daemon=True).start()
            def request(method, path, body=None, cookie=None):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                try:
                    headers = {'Content-Type': 'application/json'}
                    if cookie:
                        headers['Cookie'] = cookie
                    connection.request(method, path, json.dumps(body) if body else None, headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read()), response.getheader('Set-Cookie')
                finally:
                    connection.close()
            try:
                code, body, cookie = request('POST', '/api/chat', {'message': 'hi', 'api_model': 'gpt-5.4'})
                self.assertEqual(code, 200)
                self.assertEqual(body['model'], 'gpt-5.4')
                self.assertEqual(request('GET', '/api/status', cookie=cookie.split(';')[0])[1]['model'], 'gpt-5.4')
                self.assertEqual(request('GET', '/api/status')[1]['model'], 'gpt-4.1-mini')
                self.assertNotIn('secret-test', json.dumps(request('GET', '/api/status')[1]))
                self.assertEqual(request('POST', '/api/chat', {'message': 'hi', 'api_model': 'unlisted'})[0], 400)
            finally:
                httpd.shutdown()
                httpd.server_close()

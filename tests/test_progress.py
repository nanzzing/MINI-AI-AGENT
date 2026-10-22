"""실제 로컬 HTTP 스트림을 사용하되 모델·금융 데이터는 호출하지 않는다."""
import http.client
import json
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from mini_ai_agent.conversation import ConversationState
from mini_ai_agent.llm import ChatError
from mini_ai_agent import server


class ProgressTransportTests(unittest.TestCase):
    def setUp(self):
        self.release = Event()
        self.fail = False
        owner = self

        class FakeConversation:
            def __init__(self, **kwargs):
                self.state = ConversationState()
                self.tool_calls, self.agent_steps, self.coverage = [], [], []
                self.research = {}

            def respond(self, message, on_progress=None):
                if on_progress:
                    on_progress({'event': '모델 호출', 'round': 1, 'elapsed_ms': 0})
                    if not owner.release.wait(5):
                        raise ChatError('test gate timed out')
                    on_progress({'event': '모델 응답 수신', 'round': 1, 'duration_ms': 15})
                if owner.fail:
                    raise ChatError('모의 연결 오류')
                return '조사 완료'

        for patcher in (patch.object(server, 'OpenAIConversation', FakeConversation),
                        patch.dict(server.SESSIONS, {}, clear=True),
                        patch.object(server.Handler, 'log_message')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.httpd.chatgpt = None
        Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.release.set)
        self.client = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
        self.addCleanup(self.client.close)

    def request(self, streaming=True, message='삼성전자 단기'):
        headers = {'Content-Type': 'application/json'}
        if streaming:
            headers['Accept'] = 'application/x-ndjson'
        self.client.request('POST', '/api/chat', json.dumps({'message': message}), headers)
        return self.client.getresponse()

    def test_progress_is_flushed_while_model_is_still_waiting(self):
        response = self.request()
        self.assertEqual(response.status, 200)
        self.assertIn('application/x-ndjson', response.getheader('Content-Type'))
        self.assertIn('HttpOnly', response.getheader('Set-Cookie'))
        self.assertEqual(response.getheader('X-Accel-Buffering'), 'no')
        first = json.loads(response.readline())
        self.assertEqual(first['type'], 'progress')
        self.assertEqual(first['step']['event'], '모델 호출')
        self.assertFalse(self.release.is_set())
        self.release.set()
        rest = [json.loads(line) for line in response.read().decode().splitlines()]
        self.assertEqual([item['type'] for item in rest], ['progress', 'result'])
        self.assertEqual(rest[-1]['data']['reply'], '조사 완료')

    def test_error_after_headers_is_a_terminal_error_event(self):
        self.fail = True
        self.release.set()
        response = self.request()
        events = [json.loads(line) for line in response.read().decode().splitlines()]
        self.assertEqual(response.status, 200)
        self.assertEqual(events[-1], {'type': 'error', 'error': '모의 연결 오류'})
        self.assertFalse(any(item['type'] == 'result' for item in events))

    def test_legacy_json_and_input_validation_remain_supported(self):
        response = self.request(streaming=False)
        self.assertIn('application/json', response.getheader('Content-Type'))
        self.assertEqual(json.loads(response.read())['reply'], '조사 완료')
        response = self.request(message='')
        self.assertEqual(response.status, 400)
        self.assertIn('error', json.loads(response.read()))

    def test_broken_stream_raises_disconnect_instead_of_continuing(self):
        handler = server.Handler.__new__(server.Handler)
        handler.wfile = SimpleNamespace(write=MagicMock(side_effect=BrokenPipeError), flush=MagicMock())
        with self.assertRaises(server.ClientDisconnected):
            handler.send_chat_event({'type': 'progress', 'step': {}})


if __name__ == '__main__':
    unittest.main()

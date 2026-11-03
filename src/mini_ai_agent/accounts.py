"""Supabase Auth + RLS가 적용된 조사 저장소.

브라우저에는 임의 세션 쿠키만 전달한다. Supabase 토큰은 서버 메모리에 두며,
DB 요청에도 관리 키가 아닌 로그인 사용자의 토큰을 사용한다.
"""
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
import json
import os
import secrets
import time
from urllib.parse import urlparse
from uuid import UUID

import httpx
from dotenv import dotenv_values


class AccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def configuration(values=None):
    values = values if values is not None else {**dotenv_values(Path(__file__).resolve().parents[2] / '.env'), **os.environ}
    env = values.get('APP_ENV') or ('production' if values.get('RENDER') else 'development')
    if env not in ('development', 'production') or (values.get('RENDER') and env != 'production'):
        raise ValueError('APP_ENV는 development 또는 production이며 Render는 production이어야 합니다.')
    prefix = 'PROD' if env == 'production' else 'DEV'
    url = (values.get(prefix + '_SUPABASE_URL') or '').rstrip('/')
    key = values.get(prefix + '_SUPABASE_PUBLISHABLE_KEY') or ''
    origin = (values.get('APP_ORIGIN') or '').rstrip('/')
    if not url and not key and env == 'development':
        return None
    parsed = urlparse(url)
    if not (parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.supabase.co')
            and not parsed.username and not parsed.port and parsed.path == '' and not parsed.query and not parsed.fragment
            and key.startswith('sb_publishable_')):
        raise ValueError(f'{prefix}_SUPABASE_URL 및 publishable key를 설정해 주세요. 관리/secret 키는 사용하지 않습니다.')
    other = (values.get(('DEV' if prefix == 'PROD' else 'PROD') + '_SUPABASE_URL') or '').rstrip('/')
    if other and other == url:
        raise ValueError('개발용과 운영용 Supabase 프로젝트는 달라야 합니다.')
    origin_parts = urlparse(origin)
    if origin and (not origin_parts.netloc or origin_parts.path or origin_parts.query or origin_parts.fragment
                   or origin_parts.username or origin_parts.scheme not in ('http', 'https')):
        raise ValueError('APP_ORIGIN에는 경로 없는 사이트 주소를 입력해 주세요.')
    if env == 'production' and origin_parts.scheme != 'https':
        raise ValueError('운영 APP_ORIGIN에는 실제 HTTPS 사이트 주소가 필요합니다.')
    return {'environment': env, 'url': url, 'key': key, 'origin': origin}


@dataclass
class LoginSession:
    user: dict
    access: str
    refresh: str
    expires: float
    deadline: float = field(default_factory=lambda: time.time() + 86400)
    lock: RLock = field(default_factory=RLock)


class Accounts:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or httpx.Client(base_url=config['url'], timeout=20, follow_redirects=False)
        self.sessions = {}
        self.attempts = {}
        self.lock = RLock()

    def close(self):
        self.http.close()

    def request(self, method, path, session=None, **kwargs):
        headers = {'apikey': self.config['key'], 'Content-Type': 'application/json'}
        if session:
            headers['Authorization'] = 'Bearer ' + session.access
        try:
            response = self.http.request(method, path, headers=headers, **kwargs)
            if response.is_error:
                status = 401 if response.status_code in (401, 403) else 503
                # 응답 원문에는 인증/개인 정보가 있을 수 있으므로 출력하지 않는다.
                raise AccountError('로그인 또는 저장소 요청을 완료하지 못했어요. 인증 상태와 Supabase 설정을 확인해 주세요.', status)
            return response.json() if response.content else None
        except (httpx.HTTPError, ValueError) as error:
            raise AccountError('Supabase에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.', 503) from error

    def limit_auth(self, email):
        # 계정별 요청 제한. 운영의 대규모 트래픽에는 공유 rate limiter/WAF를 추가한다.
        now = time.time()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if v[1] > now}
            count, deadline = self.attempts.get(email.lower(), (0, now + 600))
            if count >= 10 or len(self.attempts) >= 10000:
                raise AccountError('요청이 많아요. 10분 후 다시 시도해 주세요.', 429)
            self.attempts[email.lower()] = (count + 1, deadline)

    def login(self, email, password):
        self.limit_auth(email)
        try:
            data = self.request('POST', '/auth/v1/token?grant_type=password', json={'email': email, 'password': password})
        except AccountError as error:
            raise AccountError('로그인하지 못했어요. 이메일·비밀번호와 이메일 인증 여부를 확인해 주세요.', 401) from error
        session = LoginSession({'id': data['user']['id'], 'email': data['user'].get('email', '')},
                               data['access_token'], data['refresh_token'], time.time() + data['expires_in'])
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v.deadline > time.time()}
            if len(self.sessions) >= 1000:
                raise AccountError('접속자가 많아요. 잠시 후 다시 시도해 주세요.', 503)
            self.sessions[token] = session
        return token, session

    def session(self, token):
        with self.lock:
            session = self.sessions.get(token)
        if not session or session.deadline <= time.time():
            with self.lock:
                self.sessions.pop(token, None)
            raise AccountError('로그인해 주세요. 서버 재시작 후에는 다시 로그인해야 합니다.', 401)
        with session.lock:
            if session.expires < time.time() + 60:
                data = self.request('POST', '/auth/v1/token?grant_type=refresh_token', json={'refresh_token': session.refresh})
                session.access, session.refresh = data['access_token'], data['refresh_token']
                session.expires = time.time() + data['expires_in']
            # DB의 RLS와 별개로 계정 삭제/인증 해제를 확인한다.
            user = self.request('GET', '/auth/v1/user', session)
            if user['id'] != session.user['id']:
                raise AccountError('다시 로그인해 주세요.', 401)
        return session

    def logout(self, token):
        with self.lock:
            session = self.sessions.pop(token, None)
        if session:
            try:
                self.request('POST', '/auth/v1/logout?scope=local', session)
            except AccountError:
                pass  # 로컬 세션은 외부 로그아웃 실패와 무관하게 즉시 폐기한다.

    def signup(self, email, password):
        self.limit_auth(email)
        self.request('POST', '/auth/v1/signup', json={'email': email, 'password': password})
        return {'message': '이메일 인증을 완료한 뒤 로그인해 주세요. 이미 가입했다면 로그인할 수 있습니다.'}

    def recovery(self, email):
        self.limit_auth(email)
        self.request('POST', '/auth/v1/recover', json={'email': email})
        return {'message': '가입된 이메일이면 재설정 코드가 발송됩니다. 메일의 코드를 입력해 주세요.'}

    def confirm(self, email, code):
        self.limit_auth(email)
        data = self.request('POST', '/auth/v1/verify', json={'email': email, 'token': code, 'type': 'signup'})
        if data.get('access_token'):
            self.request('POST', '/auth/v1/logout?scope=local', LoginSession({}, data['access_token'], '', 0))
        return {'message': '이메일 인증을 완료했어요. 로그인해 주세요.'}

    def password(self, email, code, password):
        self.limit_auth(email)
        data = self.request('POST', '/auth/v1/verify', json={'email': email, 'token': code, 'type': 'recovery'})
        temporary = LoginSession({}, data['access_token'], '', 0)
        self.request('PUT', '/auth/v1/user', temporary, json={'password': password})
        self.request('POST', '/auth/v1/logout?scope=global', temporary)
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v.user['id'] != data['user']['id']}
        return {'message': '비밀번호를 변경했어요. 새 비밀번호로 로그인해 주세요.'}

    def list(self, session, offset=0):
        return self.request('GET', '/rest/v1/research_threads', session, params={
            'select': 'id,title,updated_at,revision', 'order': 'updated_at.desc,id.desc', 'limit': 30, 'offset': offset})

    def create(self, session):
        rows = self.request('POST', '/rest/v1/rpc/create_research_thread', session, json={})
        return rows[0]

    def thread(self, session, identifier):
        identifier = str(UUID(identifier))
        rows = self.request('GET', '/rest/v1/research_threads', session, params={'id': 'eq.' + identifier, 'select': '*'})
        if not rows:
            raise AccountError('조사 내역을 찾을 수 없어요.', 404)
        return rows[0]

    def turns(self, session, identifier, offset=0):
        self.thread(session, identifier)
        return self.request('GET', '/rest/v1/research_turns', session, params={
            'thread_id': 'eq.' + identifier, 'order': 'sequence.desc', 'limit': 30, 'offset': offset, 'select': '*'})

    def delete(self, session, identifier):
        self.thread(session, identifier)
        self.request('DELETE', '/rest/v1/research_threads', session, params={'id': 'eq.' + identifier})

    def save(self, session, thread, question, result, context):
        # 응답 및 근거만 보존한다. 전체 OHLCV/재무 원본과 상세 도구 반환은 중복 저장하지 않는다.
        snapshot = {k: result[k] for k in ('reply', 'company', 'code', 'exchange', 'horizon', 'stage', 'focus', 'coverage', 'model') if k in result}
        payload = {'p_thread': thread['id'], 'p_revision': thread['revision'], 'p_question': question,
                   'p_result': snapshot, 'p_context': context}
        if len(json.dumps(payload).encode()) > 2_000_000:
            raise AccountError('근거 자료가 너무 커서 저장하지 못했어요.', 413)
        return self.request('POST', '/rest/v1/rpc/append_research_turn', session, json=payload)

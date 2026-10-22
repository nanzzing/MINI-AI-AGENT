"""공식 Sign in with ChatGPT의 로컬 개인용 연결.

브라우저 로그인 -> state/PKCE/ID token 검증 -> 보호 저장 -> 모델 목록 -> Responses.
API 키 결제 경로와 분리하며, 구독 오류 때 API 키로 자동 전환하지 않는다.
"""
import base64
from copy import deepcopy
import hashlib
import secrets
from threading import RLock
import time
from urllib.parse import urlencode, urlparse
import uuid

import httpx
import jwt
from openai import OpenAI, APIStatusError, APIConnectionError

from mini_ai_agent.auth_storage import CredentialStore

ISSUER = 'https://auth.openai.com'
AUTHORIZE_URL = ISSUER + '/api/accounts/authorize'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
USAGE_URL = 'https://chatgpt.com/settings/usage'
TOKEN_FIELDS = ('access_token', 'refresh_token', 'id_token', 'expires_at', 'scopes')
TERMINAL_REFRESH_ERRORS = {'invalid_grant', 'invalid_refresh_token', 'token_expired',
                          'refresh_token_expired', 'refresh_token_invalidated', 'refresh_token_reused'}


class ChatGPTError(Exception):
    """인증 응답 원문/토큰이 아닌 사용자에게 공개해도 되는 메시지."""


def _permission(profile):
    return bool(profile.get('access_token') and 'chatgpt.tokens.use.direct' in profile.get('scopes', []))


def _validate_identity(token, client_id, nonce=None):
    # 임의의 JWT payload 디코딩을 로그인 확인으로 취급하지 않는다.
    try:
        keys = jwt.PyJWKClient(ISSUER + '/.well-known/jwks.json', timeout=15)
        key = keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256'], audience=client_id, issuer=ISSUER,
                            options={'require': ['exp', 'iat', 'sub', 'iss', 'aud']})
        if not isinstance(claims['sub'], str) or not claims['sub']:
            raise ValueError
        if nonce is not None and not secrets.compare_digest(str(claims.get('nonce', '')), nonce):
            raise ValueError
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, OSError) as error:
        raise ChatGPTError('ChatGPT 로그인 서명을 확인하지 못했어요. 다시 로그인해 주세요.') from error


def subscription_error(code=None, status=None):
    if code == 'subscription_sharing_usage_limit_exceeded' or status == 429:
        return 'ChatGPT 구독 또는 이 앱의 사용 한도에 도달했어요. 사용량 관리에서 확인해 주세요. API 결제로 전환하지 않았습니다.'
    if code == 'subscription_sharing_user_not_eligible':
        return '선택한 ChatGPT 계정·워크스페이스는 구독 연결 대상이 아니에요. Pro 계정과 선택한 워크스페이스를 확인해 주세요.'
    if code == 'subscription_sharing_unsupported_capability' or status == 400:
        return 'ChatGPT 구독 경로가 이 모델 또는 요청 기능을 지원하지 않아요. 다른 제공 모델을 선택해 주세요.'
    if status == 401 or code == 'subscription_sharing_invalid_user':
        return 'ChatGPT 인증이 만료되었거나 해제됐어요. 계정을 다시 연결해 주세요.'
    if status == 403:
        return 'ChatGPT 구독 사용 권한 또는 지역·워크스페이스 정책을 확인해 주세요.'
    return 'ChatGPT 구독 요청을 완료하지 못했어요. 잠시 후 다시 시도해 주세요. API 결제로 전환하지 않았습니다.'


class ChatGPTConnection:
    def __init__(self, store=None, http=None):
        self.store = store or CredentialStore()
        self.http = http or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self.pending = {}
        self.lock = RLock()
        self.login_error = ''

    def close(self):
        self.http.close()

    def _expire_attempts(self):
        expired = [state for state, attempt in self.pending.items() if attempt['expires'] < time.time()]
        for state in expired:
            del self.pending[state]
        if expired:
            self.login_error = '로그인 대기 시간이 지났어요. 다시 연결해 주세요.'

    def status(self):
        with self.lock, self.store.locked() as data:
            self._expire_attempts()
            profile = data['profiles'].get(data['active'], {})
            # 허용 목록만 반환한다. 토큰·subject·인증 URL은 브라우저 상태에 넣지 않는다.
            return {'provider': 'chatgpt', 'configured': _permission(profile) and bool(profile.get('model')),
                    'connected': bool(profile.get('access_token')), 'plan_enabled': _permission(profile),
                    'active_profile': data['active'], 'model': profile.get('model', ''),
                    'models': profile.get('models', []), 'usage_url': USAGE_URL,
                    'login_pending': bool(self.pending), 'login_error': self.login_error,
                    'profiles': [{'id': key, 'label': value.get('email') or '연결 대기',
                                  'connected': bool(value.get('access_token'))}
                                 for key, value in data['profiles'].items()]}

    def begin_login(self, redirect_uri, profile_id=None):
        parsed = urlparse(redirect_uri)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path != '/auth/callback':
            raise ChatGPTError('ChatGPT 로그인은 127.0.0.1 로컬 서버에서만 사용할 수 있어요.')
        with self.lock, self.store.locked() as data:
            self._expire_attempts()
            if len(self.pending) >= 3:
                raise ChatGPTError('이미 로그인이 진행 중이에요. 열린 로그인 창을 확인해 주세요.')
            if profile_id and profile_id not in data['profiles']:
                raise ChatGPTError('저장된 계정을 찾을 수 없어요.')
            profile = data['profiles'].get(profile_id, {})
            data['host_id'] = data['host_id'] or 'urn:uuid:' + str(uuid.uuid4())
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            client_id = profile.get('client_id', 'dynamic_agent_client')
            self.pending[state] = {'nonce': nonce, 'verifier': verifier, 'redirect_uri': redirect_uri,
                                   'client_id': client_id, 'profile_id': profile_id,
                                   'subject': profile.get('subject'), 'expires': time.time() + 600}
            params = {'client_id': client_id, 'ext_agent_host_id': data['host_id'],
                      'response_type': 'code', 'redirect_uri': redirect_uri, 'scope': SCOPES,
                      'resource': RESOURCE, 'state': state, 'nonce': nonce,
                      'code_challenge_method': 'S256',
                      'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
            if client_id == 'dynamic_agent_client':
                params['agent_name_hint'] = 'MINI-AI-AGENT'
            elif profile.get('email'):
                params['login_hint'] = profile['email']
            self.login_error = ''
            return AUTHORIZE_URL + '?' + urlencode(params)

    def _post_token(self, form):
        try:
            response = self.http.post(TOKEN_URL, data={**form, 'resource': RESOURCE})
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ChatGPTError('ChatGPT 인증 서버와 연결하지 못했어요. 잠시 후 다시 시도해 주세요.') from error
        if not isinstance(payload, dict):
            raise ChatGPTError('ChatGPT 인증 응답을 확인하지 못했어요.')
        return response.status_code, payload

    def _credentials(self, payload, previous=None):
        previous = previous or {}
        if (not isinstance(payload.get('access_token'), str) or not payload['access_token']
                or str(payload.get('token_type', '')).lower() != 'bearer'):
            raise ChatGPTError('ChatGPT 접근 권한을 확인하지 못했어요.')
        try:
            expires = int(payload['expires_in'])
            if expires <= 0:
                raise ValueError
        except (KeyError, ValueError, TypeError) as error:
            raise ChatGPTError('ChatGPT 인증 만료 시간을 확인하지 못했어요.') from error
        scope = payload.get('scope')
        if scope is not None and not isinstance(scope, str):
            raise ChatGPTError('ChatGPT 사용 권한 형식을 확인하지 못했어요.')
        result = {field: previous[field] for field in TOKEN_FIELDS if field in previous}
        result.update(access_token=payload['access_token'], expires_at=time.time() + expires,
                      scopes=scope.split() if scope is not None else previous.get('scopes', []))
        for name in ('refresh_token', 'id_token'):
            if isinstance(payload.get(name), str) and payload[name]:
                result[name] = payload[name]
        return result

    def complete_login(self, params):
        with self.lock:
            self._expire_attempts()
            state = params.get('state', '')
            attempt = self.pending.pop(state, None)
            if not attempt:
                raise ChatGPTError('로그인 요청이 만료되었거나 일치하지 않아요. MINI에서 다시 연결해 주세요.')
            try:
                self._complete_login(params, attempt)
                self.login_error = ''
            except ChatGPTError as error:
                self.login_error = str(error)
                raise

    def _complete_login(self, params, attempt):
        if params.get('error'):
            raise ChatGPTError('ChatGPT 로그인이 취소됐어요. 구독 사용을 허용하려면 다시 연결해 주세요.')
        issued = params.get('client_id') or attempt['client_id']
        if (not isinstance(issued, str) or not issued or issued == 'dynamic_agent_client'
                or len(issued) > 200 or not params.get('code')):
            raise ChatGPTError('ChatGPT 앱 등록이 완료되지 않았어요. 다시 연결해 주세요.')
        if attempt['client_id'] != 'dynamic_agent_client' and issued != attempt['client_id']:
            raise ChatGPTError('선택한 계정과 로그인 등록 정보가 일치하지 않아요.')
        profile_id = attempt['profile_id'] or str(uuid.uuid4())
        # 코드 교환 실패에도 발급받은 client_id는 남겨 재시도 때 새 등록을 만들지 않는다.
        with self.store.locked() as data:
            data['profiles'].setdefault(profile_id, {'client_id': issued})
        status, payload = self._post_token({'grant_type': 'authorization_code', 'client_id': issued,
                                           'code': params['code'], 'code_verifier': attempt['verifier'],
                                           'redirect_uri': attempt['redirect_uri']})
        if status != 200:
            raise ChatGPTError('로그인 코드 교환에 실패했어요. 저장된 연결을 선택해 다시 로그인해 주세요.')
        claims = _validate_identity(payload.get('id_token', ''), issued, attempt['nonce'])
        if attempt['subject'] and claims['sub'] != attempt['subject']:
            raise ChatGPTError('기존 연결과 다른 계정이에요. 새 계정 연결을 선택해 주세요.')
        credentials = self._credentials(payload)
        with self.store.locked() as data:
            data['profiles'][profile_id].update(credentials, subject=claims['sub'],
                                               email=str(claims.get('email', '')), issuer=ISSUER,
                                               revision=secrets.token_urlsafe(16), models=[], model='')
            data['active'] = profile_id

    def _access_token(self, profile_id, revision=None):
        failure = None
        token = None
        # 파일 잠금을 유지한 채 갱신하고 새 refresh token과 만료 시각을 함께 저장한다.
        with self.store.locked() as data:
            profile = data['profiles'].get(profile_id, {})
            if data['active'] != profile_id or (revision and revision != profile.get('revision')):
                raise ChatGPTError('ChatGPT 계정 또는 모델이 변경됐어요. 질문을 다시 보내 주세요.')
            if not _permission(profile):
                raise ChatGPTError('ChatGPT에 로그인하고 이 앱의 구독 사용을 허용해 주세요.')
            if profile.get('expires_at', 0) <= time.time() + 60:
                if not profile.get('refresh_token'):
                    raise ChatGPTError('ChatGPT 로그인 유효기간이 지났어요. 다시 연결해 주세요.')
                status, payload = self._post_token({'grant_type': 'refresh_token',
                                                   'client_id': profile['client_id'],
                                                   'refresh_token': profile['refresh_token']})
                if status != 200:
                    if isinstance(payload.get('error'), str) and payload['error'] in TERMINAL_REFRESH_ERRORS:
                        for field in TOKEN_FIELDS:
                            profile.pop(field, None)
                    failure = 'ChatGPT 인증을 갱신하지 못했어요. 다시 연결해 주세요.'
                else:
                    if payload.get('id_token'):
                        claims = _validate_identity(payload['id_token'], profile['client_id'])
                        if claims['sub'] != profile['subject']:
                            raise ChatGPTError('갱신된 ChatGPT 계정이 기존 계정과 달라요.')
                    profile.update(self._credentials(payload, profile))
            if not failure:
                if not _permission(profile):
                    failure = 'ChatGPT 구독 사용 권한이 해제됐어요. 다시 연결해 주세요.'
                token = profile.get('access_token')
        # 영구 갱신 오류의 토큰 삭제가 저장된 다음 예외를 발생시킨다.
        if failure:
            raise ChatGPTError(failure)
        return token

    def refresh_models(self):
        with self.lock:
            with self.store.locked() as data:
                profile_id = data['active']
            token = self._access_token(profile_id)
            try:
                response = self.http.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token})
                if response.status_code != 200:
                    raise ChatGPTError(subscription_error(status=response.status_code))
                payload = response.json()
                models = [{'slug': model['slug'], 'display_name': model.get('display_name') or model['slug']}
                          for model in payload.get('models', []) if isinstance(model, dict)
                          and model.get('visibility') == 'list' and isinstance(model.get('slug'), str)]
            except (httpx.HTTPError, ValueError, AttributeError) as error:
                raise ChatGPTError('ChatGPT 모델 목록을 가져오지 못했어요.') from error
            if not models:
                raise ChatGPTError('이 계정에서 사용할 수 있는 구독 모델이 없어요.')
            with self.store.locked() as data:
                if data['active'] != profile_id:
                    raise ChatGPTError('계정이 변경됐어요. 모델 목록을 다시 불러와 주세요.')
                profile = data['profiles'][profile_id]
                profile['models'] = models
                if profile.get('model') not in {model['slug'] for model in models}:
                    profile['model'] = models[0]['slug']
            return self.status()

    def select(self, profile_id=None, model=None):
        with self.lock, self.store.locked() as data:
            if profile_id is not None:
                if profile_id not in data['profiles']:
                    raise ChatGPTError('저장된 계정을 찾을 수 없어요.')
                data['active'] = profile_id
            profile = data['profiles'].get(data['active'], {})
            if model is not None:
                if model not in {item['slug'] for item in profile.get('models', [])}:
                    raise ChatGPTError('현재 계정의 모델 목록에서 선택해 주세요.')
                profile['model'] = model
            profile['revision'] = secrets.token_urlsafe(16)

    def logout(self):
        revoked = True
        with self.lock, self.store.locked() as data:
            self.pending.clear()
            self.login_error = ''
            profile = data['profiles'].get(data['active'], {})
            refresh = profile.get('refresh_token')
            if refresh:
                try:
                    discovery = self.http.get(ISSUER + '/.well-known/openid-configuration').json()
                    endpoint = discovery.get('revocation_endpoint', '')
                    # 검색된 엔드포인트가 다른 호스트로 토큰을 보내도록 만들 수 없게 제한한다.
                    if urlparse(endpoint).scheme != 'https' or urlparse(endpoint).netloc != 'auth.openai.com':
                        raise ValueError
                    for attempt in range(2):
                        response = self.http.post(endpoint, data={'token': refresh,
                            'token_type_hint': 'refresh_token', 'client_id': profile['client_id']})
                        if response.status_code < 500:
                            break
                        if attempt == 0:
                            time.sleep(0.25)
                    revoked = response.status_code == 200
                except (httpx.HTTPError, ValueError, AttributeError):
                    revoked = False
            for field in TOKEN_FIELDS:
                profile.pop(field, None)
            profile.update(revision=secrets.token_urlsafe(16), models=[], model='')
        return {'message': '로그아웃했어요.' if revoked else
                '로컬에서 로그아웃했지만 원격 연결 해제는 확인하지 못했어요. ChatGPT 설정에서 이 앱을 연결 해제해 주세요.'}

    def client(self):
        with self.lock, self.store.locked() as data:
            profile_id = data['active']
            profile = data['profiles'].get(profile_id, {})
            if not _permission(profile):
                raise ChatGPTError('Continue with ChatGPT로 로그인하고 구독 사용을 허용해 주세요.')
            if not profile.get('model'):
                raise ChatGPTError('ChatGPT 모델 목록을 새로 불러온 뒤 모델을 선택해 주세요.')
            return SubscriptionClient(self, profile_id, profile['revision']), profile['model']


class SubscriptionClient:
    """기존 Agent Loop와 같은 create() 계약, 전송만 구독용 HTTP/SSE로 변환한다."""
    def __init__(self, connection, profile_id, revision):
        self.connection, self.profile_id, self.revision = connection, profile_id, revision
        self.responses = self

    def create(self, **request):
        token = self.connection._access_token(self.profile_id, self.revision)
        request = deepcopy(request)
        request.pop('max_output_tokens', None)  # SIWC 미지원. API 키 경로의 제한은 유지한다.
        tools = request.pop('tools', [])
        choice = request.get('tool_choice')
        if isinstance(choice, dict) and choice.get('type') == 'function':
            tools = [tool for tool in tools if tool['name'] == choice['name']]
            request['tool_choice'] = 'required'
        request['tools'] = [{'type': 'namespace', 'name': 'research',
                             'description': 'MINI 주식 조사 도구', 'tools': tools}] if tools else []
        request.update(store=False, stream=True, include=['reasoning.encrypted_content'])
        try:
            with OpenAI(api_key=token, base_url=RESOURCE, timeout=90, max_retries=0) as client:
                with client.responses.create(**request) as stream:
                    completed_items = {}
                    for event in stream:
                        # 구독 스트림은 완료 이벤트의 output이 비어 있을 수 있다.
                        # delta가 아니라 완성된 항목을 순서대로 보관하고, 전체 완료가
                        # 확인된 뒤에만 공개한다. reasoning도 후속 왕복을 위해 보존한다.
                        if event.type == 'response.output_item.done':
                            completed_items[event.output_index] = event.item
                        if event.type == 'response.completed':
                            if not event.response.output and completed_items:
                                event.response.output = [completed_items[index] for index in sorted(completed_items)]
                            for item in event.response.output:
                                if item.type == 'function_call' and getattr(item, 'namespace', None) not in (None, 'research'):
                                    raise ChatGPTError('허용되지 않은 도구 영역의 요청을 받았어요.')
                            # 도중 로그아웃/계정 전환이 일어나면 이전 연결의 결과를 쓰지 않는다.
                            self.connection._access_token(self.profile_id, self.revision)
                            return event.response
                        if event.type in {'response.failed', 'error'}:
                            error = getattr(getattr(event, 'response', None), 'error', None)
                            raise ChatGPTError(subscription_error(getattr(error, 'code', None) or getattr(event, 'code', None)))
                        if event.type == 'response.incomplete':
                            raise ChatGPTError('ChatGPT 응답이 중간에 끝났어요. 질문을 줄여 다시 시도해 주세요.')
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            detail = body.get('error', body)
            code = detail.get('code') if isinstance(detail, dict) else None
            raise ChatGPTError(subscription_error(code, error.status_code)) from error
        except (APIConnectionError, httpx.HTTPError) as error:
            raise ChatGPTError('ChatGPT 구독 연결이 끊겼어요. 네트워크를 확인해 주세요.') from error
        raise ChatGPTError('ChatGPT 응답 완료 신호를 받지 못했어요. 다시 시도해 주세요.')

    def close(self):
        pass  # HTTP 클라이언트는 호출별 with 블록에서 닫는다.

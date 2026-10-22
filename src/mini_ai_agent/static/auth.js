/* 브라우저에는 계정 표시 정보와 CSRF 값만 둔다. OAuth 토큰은 Python에만 있다. */
window.chatgptAuth = (() => {
  let csrf = '';
  let current = null;
  let working = false;
  let chatBusy = false;
  let pollTimer = null;
  let pollDeadline = 0;
  let pendingLogin = false;
  const element = (id) => document.getElementById(id);
  const root = element('chatgpt-controls');

  function note(message, failed = false) {
    element('auth-message').textContent = message;
    element('auth-message').classList.toggle('error', failed);
  }

  function options(select, rows, selected) {
    select.replaceChildren(...rows.map(({ value, label }) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.selected = value === selected;
      return option;
    }));
  }

  function render(data) {
    current = data;
    csrf = data.csrf || csrf;
    root.hidden = data.provider !== 'chatgpt';
    if (root.hidden) return;
    element('auth-state').textContent = data.plan_enabled ? 'ChatGPT 구독 사용' : data.connected ? '구독 사용 동의 필요' : '로그인 필요';
    options(element('auth-profile'), [{ value: '', label: '새 계정 연결' },
      ...(data.profiles || []).map((profile, index) => ({ value: profile.id,
        label: `${profile.label} · 연결 ${index + 1}${profile.connected ? '' : ' (로그아웃)'}` }))], data.active_profile || '');
    options(element('auth-model'), data.models?.length ? data.models.map((model) => ({ value: model.slug, label: model.display_name }))
      : [{ value: '', label: '모델 선택' }], data.model || '');
    element('auth-login').textContent = data.connected ? 'ChatGPT 다시 연결' : 'Continue with ChatGPT';
    root.querySelectorAll('button, select').forEach((control) => { control.disabled = working || chatBusy; });
    element('auth-model').disabled ||= !data.models?.length;
    element('auth-models').disabled ||= !data.plan_enabled;
    element('auth-logout').disabled ||= !data.connected && !data.login_pending;
    if (data.login_error) note(data.login_error, true);
  }

  async function status() {
    const response = await fetch('/api/status');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '연결 상태를 확인하지 못했어요.');
    render(data);
    window.dispatchEvent(new CustomEvent('mini-auth-status', { detail: data }));
    return data;
  }

  async function post(path, body = {}) {
    if (!csrf) await status();
    const response = await fetch(path, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Mini-CSRF': csrf,
    }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'ChatGPT 연결 요청에 실패했어요.');
    return data;
  }

  async function action(callback) {
    if (working || chatBusy) return;
    working = true;
    if (current) render(current);
    note('');
    try { await callback(); }
    catch (error) { note(error.message, true); }
    finally {
      working = false;
      try { await status(); } catch (error) { note(error.message, true); }
    }
  }

  async function pollLogin() {
    try {
      const data = await status();
      if (!data.login_pending) {
        pendingLogin = false;
        if (data.login_error) return;
        element('auth-fallback').hidden = true;
        if (data.plan_enabled) {
          try { await post('/api/auth/models'); await status(); }
          catch (error) { note(error.message, true); }
          // 동의 직후 한 번만 구독 사용을 확인한다. 일반 새로고침 때는 열지 않는다.
          element('auth-welcome').showModal();
          window.dispatchEvent(new Event('mini-account-change'));
        } else {
          note('계정은 연결됐지만 구독 사용이 허용되지 않았어요. 다시 연결해 권한을 확인해 주세요.', true);
        }
        return;
      }
    } catch (error) { note(error.message, true); }
    if (pendingLogin && Date.now() < pollDeadline) pollTimer = setTimeout(pollLogin, 1500);
    else pendingLogin = false;
  }

  element('auth-login').addEventListener('click', () => {
    if (working || chatBusy) return;
    const selected = element('auth-profile').value;
    // 클릭 순간 창을 열어 팝업 차단을 줄인다. 실패하면 같은 공식 URL의 링크를 남긴다.
    const popup = window.open('about:blank', '_blank');
    if (popup) popup.opener = null;
    action(async () => {
      const result = await post('/api/auth/login', { profile_id: selected || null });
      const url = new URL(result.authorization_url);
      if (url.origin !== 'https://auth.openai.com' || url.pathname !== '/api/accounts/authorize') throw new Error('로그인 주소를 확인하지 못했어요.');
      if (popup) popup.location.replace(url.href);
      element('auth-fallback').href = url.href;
      element('auth-fallback').hidden = false;
      note('열린 ChatGPT 창에서 로그인과 구독 사용 동의를 완료해 주세요.');
      pendingLogin = true;
      pollDeadline = Date.now() + 610000;
      clearTimeout(pollTimer);
      pollTimer = setTimeout(pollLogin, 1500);
    });
  });
  element('auth-models').addEventListener('click', () => action(async () => {
    await post('/api/auth/models');
  }));
  element('auth-profile').addEventListener('change', () => {
    const selected = element('auth-profile').value;
    if (!selected) return;
    action(async () => {
      await post('/api/auth/select', { profile_id: selected });
      window.dispatchEvent(new Event('mini-account-change'));
    });
  });
  element('auth-model').addEventListener('change', () => {
    const model = element('auth-model').value;
    action(async () => {
      await post('/api/auth/select', { model });
      window.dispatchEvent(new Event('mini-account-change'));
    });
  });
  element('auth-logout').addEventListener('click', () => action(async () => {
    pendingLogin = false;
    clearTimeout(pollTimer);
    element('auth-fallback').hidden = true;
    const result = await post('/api/auth/logout');
    note(result.message);
    window.dispatchEvent(new Event('mini-account-change'));
  }));
  element('auth-welcome-close').addEventListener('click', () => element('auth-welcome').close());
  return { render, headers: () => csrf ? { 'X-Mini-CSRF': csrf } : {},
    setChatBusy(value) { chatBusy = value; if (current) render(current); } };
})();

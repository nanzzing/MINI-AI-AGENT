/* 사이트 계정은 AI 공급자 계정과 별개다. 비밀번호/토큰을 localStorage에 저장하지 않는다. */
(() => {
  const el = id => document.getElementById(id);
  let enabled = true, user = null, threadId = null, rows = [], mode = 'login', locked = false;
  const dialog = el('account-dialog');
  const icon = name => { const i = document.createElement('i'); i.dataset.lucide = name; return i; };
  const status = message => document.querySelectorAll('.history-status').forEach(n => n.textContent = message);
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json', 'X-Mini-Account':'1', ...window.chatgptAuth?.headers()},
      body:JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '요청을 처리하지 못했어요.');
    return data;
  }
  function resetView() {
    threadId = null;
    window.researchHistory?.clear();
    renderList();
  }
  function renderAccount() {
    el('account-open').querySelector('span').textContent = user?.email || '로그인';
    el('account-open').title = user?.email || '로그인';
    el('account-logout').hidden = !user;
    status(!enabled ? 'Supabase 개발용 설정 필요' : !user ? '로그인 후 조사 기록을 볼 수 있어요.' : rows.length ? '' : '저장된 조사가 없습니다.');
  }
  async function refresh() {
    const data = await request('/api/account');
    enabled = data.enabled; user = data.user;
    renderAccount();
    if (user) await loadListAfterLogin();
  }
  async function loadListAfterLogin() {
    // 인증 성공과 저장소 조회 실패를 분리해 로그인 재시도를 유도하지 않는다.
    try { await loadList(); }
    catch (error) { status('로그인되어 있지만 조사 기록을 불러오지 못했어요. ' + error.message); }
  }
  async function loadList(more = false) {
    const data = await request('/api/threads?offset=' + (more ? rows.length : 0));
    rows = more ? [...rows, ...data.threads] : data.threads;
    renderList(); renderAccount();
    document.querySelectorAll('.history-more').forEach(n => n.hidden = data.threads.length < 30);
  }
  function renderList() {
    for (const root of [el('research-list'), el('research-list-mobile')]) {
      root.replaceChildren();
      for (const row of rows) {
        const item = document.createElement('div'); item.className = 'history-row' + (row.id === threadId ? ' is-active' : '');
        const open = document.createElement('button'); open.className = 'history-select';
        const title = document.createElement('strong'); title.textContent = row.title; open.title = row.title;
        const date = document.createElement('small'); date.textContent = new Date(row.updated_at).toLocaleString('ko-KR');
        open.append(title, date); open.addEventListener('click', () => guarded(() => openThread(row.id)));
        const remove = document.createElement('button'); remove.className = 'history-delete';
        remove.title = '조사 삭제'; remove.setAttribute('aria-label', row.title + ' 삭제'); remove.append(icon('trash-2'));
        remove.addEventListener('click', () => guarded(async () => {
          if (!confirm('이 조사와 대화 기록을 삭제할까요?')) return;
          await request('/api/threads/delete', {id:row.id});
          if (threadId === row.id) resetView();
          await loadList();
        }));
        item.append(open, remove); root.append(item);
      }
    }
    window.lucide?.createIcons();
  }
  async function guarded(action) {
    if (locked || window.researchHistory?.busy()) return;
    locked = true;
    try { await action(); } catch (error) { status(error.message); }
    finally { locked = false; }
  }
  async function openThread(id) {
    const data = await request('/api/thread?id=' + encodeURIComponent(id));
    threadId = id;
    window.researchHistory.show(data.turns.slice().reverse(), data.thread, data.turns.length === 30);
    el('history-dialog').close(); renderList();
  }
  function chooseMode(value) {
    mode = value;
    document.querySelectorAll('[data-account-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.accountMode === mode)));
    el('account-password-label').hidden = ['recovery','confirm'].includes(mode);
    el('account-password').required = !['recovery','confirm'].includes(mode);
    el('account-password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
    el('account-code-label').hidden = !['password','confirm'].includes(mode);
    el('account-code').required = ['password','confirm'].includes(mode);
    el('account-submit').textContent = {login:'로그인',signup:'가입하기',confirm:'이메일 인증',recovery:'재설정 코드 받기',password:'비밀번호 변경'}[mode];
    el('account-error').textContent = '';
  }
  document.querySelectorAll('[data-account-mode]').forEach(b => b.addEventListener('click', () => chooseMode(b.dataset.accountMode)));
  document.querySelectorAll('.dialog-close').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
  el('account-open').addEventListener('click', () => { if (!user) dialog.showModal(); });
  el('history-open').addEventListener('click', () => el('history-dialog').showModal());
  el('account-logout').addEventListener('click', () => guarded(async () => {
    await request('/api/account/logout', {}); user = null; rows = []; resetView(); renderAccount();
    document.querySelectorAll('.history-more').forEach(n => n.hidden = true);
  }));
  document.querySelectorAll('.history-more').forEach(b => b.addEventListener('click', () => guarded(() => loadList(true))));
  el('account-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (locked) return;
    locked = true; el('account-submit').disabled = true;
    try {
      const data = await request('/api/account/' + mode, {email:el('account-email').value.trim(), password:el('account-password').value, code:el('account-code').value.trim()});
      if (mode === 'login') {
        user = data.user; rows = []; resetView(); renderAccount(); dialog.close();
        el('account-password').value = ''; el('account-code').value = '';
        await loadListAfterLogin();
      } else {
        if (mode === 'recovery') chooseMode('password');
        else if (mode === 'signup') chooseMode('confirm');
        else chooseMode('login');
        el('account-error').textContent = data.message;
      }
      el('account-password').value = ''; el('account-code').value = '';
    } catch (error) { el('account-error').textContent = error.message; }
    finally { locked = false; el('account-submit').disabled = false; }
  });
  const ready = refresh().catch(error => { status(error.message); });
  window.siteAccount = {
    ready,
    headers: () => ({'X-Mini-Account':'1'}),
    async prepare() {
      await ready;
      if (locked) throw new Error('계정 또는 조사 내역을 불러오는 중이에요. 잠시 후 다시 시도해 주세요.');
      if (!enabled) return null;
      if (!user) { dialog.showModal(); throw new Error('로그인 후 질문해 주세요.'); }
      if (!threadId) {
        const data = await request('/api/threads/create', {}); threadId = data.thread.id; await loadList();
      }
      return threadId;
    },
    async newResearch() {
      await ready;
      if (!enabled) return false;
      if (!locked && !window.researchHistory.busy()) resetView();
      return true;
    },
    async saved(data) { if (data.thread_id) { threadId = data.thread_id; await loadList(); } },
    async older(offset) { return request('/api/thread?id=' + encodeURIComponent(threadId) + '&offset=' + offset); },
  };
})();

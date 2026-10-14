const $ = (selector) => document.querySelector(selector);
let busy = false;
let selectedHorizon = null;
const icons = () => window.lucide?.createIcons();

async function refreshStatus() {
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error('status');
    const data = await response.json();
    $('.connection').textContent = data.configured ? `OpenAI · ${data.model}` : 'OpenAI · API 키 설정 필요';
    $('.data-note').textContent = 'Yahoo Finance 가격·재무 / Google News · 제공 시점 기준';
  } catch {
    $('.connection').textContent = '서버 연결 확인 필요';
  }
}

function addMessage(text, type = 'assistant') {
  $('#welcome')?.remove();
  const node = document.createElement('div');
  node.className = `message ${type}`;
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = type === 'user' ? 'YOU' : 'MINI / RESEARCH ASSISTANT';
  node.append(label, document.createTextNode(text));
  $('#messages').append(node);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  return node;
}

function renderContext(data) {
  $('#sidebar-company').textContent = data.company || '선택한 종목 없음';
  $('#sidebar-horizon').textContent = data.horizon ? `${data.horizon} 관점` : '투자 관점 미정';
  $('#context-company').textContent = data.company || '조사 컨텍스트';
  $('#context-code').textContent = data.code ? `${data.code} · ${data.exchange}` : '종목 미선택';
  $('#context-horizon').textContent = data.horizon || '관점 미정';
  selectedHorizon = data.horizon === '중장기' ? '중기' : data.horizon;
  document.querySelectorAll('[data-horizon]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.horizon === selectedHorizon)));
  $('#criteria').replaceChildren();
  for (const item of data.focus) {
    const row = document.createElement('li');
    row.textContent = item;
    $('#criteria').append(row);
  }
  if (!data.focus.length) {
    const row = document.createElement('li');
    row.className = 'empty-criteria';
    row.textContent = '투자 관점을 선택하면 조사 항목이 표시됩니다.';
    $('#criteria').append(row);
  }
  $('#criteria-count').textContent = data.focus.length ? `${data.focus.length}개 항목` : '—';
  marketView.context(data);
}

async function send(message, reset = false) {
  if (busy || !message.trim()) return;
  busy = true;
  document.querySelectorAll('button').forEach(button => button.disabled = true);
  $('#composer-status').textContent = '응답을 준비하고 있어요…';
  if (!reset) addMessage(message, 'user');
  if (reset) marketView.clear();
  // 명확히 입력된 종목의 차트는 모델 응답을 기다리지 않고 조회한다.
  if (!reset && !/^(단기|중기|중장기|장기)$/.test(message.trim())) marketView.load({query:message,...(selectedHorizon?{horizon:selectedHorizon}:{})});
  try {
    const response = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '요청을 처리하지 못했어요.');
    if (reset) $('#messages').replaceChildren();
    addMessage(data.reply);
    if (!reset) $('.connection').textContent = 'OpenAI · 응답 수신 완료';
    renderContext(data);
    $('#message').value = '';
  } catch (error) {
    addMessage(error.message, 'error');
    refreshStatus();
  } finally {
    busy = false;
    document.querySelectorAll('button').forEach(button => button.disabled = false);
    $('#composer-status').textContent = '종목 · 투자 관점 · 궁금한 점';
    $('#message').focus();
  }
}

$('#chat-form').addEventListener('submit', event => {
  event.preventDefault();
  let message = $('#message').value.trim();
  if (message && selectedHorizon && !/(단기|중기|중장기|장기)/.test(message)) message += ` (${selectedHorizon} 관점)`;
  send(message);
});
$('#message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('#chat-form').requestSubmit();
  }
});
document.querySelectorAll('[data-query]').forEach(button => button.addEventListener('click', () => send(button.dataset.query)));
document.querySelectorAll('[data-horizon]').forEach(button => button.addEventListener('click', () => send(button.dataset.horizon)));
$('#new-chat').addEventListener('click', () => send('/reset', true));

icons();
refreshStatus();

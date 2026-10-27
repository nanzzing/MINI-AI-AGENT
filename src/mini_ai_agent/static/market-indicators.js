// 화면 전용 공통 지표: 채팅/모델 호출 없이 독립 갱신한다.
(() => {
  // 네이티브 dialog로 키보드 포커스와 Escape 닫기를 함께 지원한다.
  document.querySelectorAll('[data-help]').forEach(button => {
    button.addEventListener('click', () => document.getElementById(button.dataset.help).showModal());
  });
  const elements = [...document.querySelectorAll('[data-indicator]')];
  let loading = false;
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    await Promise.all(elements.map(async node => {
      try {
        const response = await fetch(`/api/market-indicator?name=${node.dataset.indicator}`, {
          signal: AbortSignal.timeout(30000), cache: 'no-store',
        });
        if (!response.ok) throw new Error('request');
        const data = await response.json();
        const isSentiment = node.dataset.indicator === 'fear_greed';
        const available = Number.isFinite(data.value) && ['ok', 'stale'].includes(data.status)
          && (!isSentiment || (data.value >= 0 && data.value <= 100));
        node.querySelector('.pulse-value').textContent = available
          ? data.value.toLocaleString('ko-KR', {maximumFractionDigits: isSentiment ? 0 : 2}) : '—';
        const dial = node.querySelector('.pulse-dial');
        // 고정 범위가 있는 CNN 점수만 게이지다. VIX/버핏은 단순 숫자 테두리다.
        dial.classList.toggle('is-gauge', isSentiment && available);
        if (isSentiment && available) {
          dial.style.setProperty('--score-angle', `${data.value * 3.6}deg`);
          dial.setAttribute('role', 'meter');
          dial.setAttribute('aria-label', 'CNN 주식 공포·탐욕지수');
          dial.setAttribute('aria-valuemin', '0');
          dial.setAttribute('aria-valuemax', '100');
          dial.setAttribute('aria-valuenow', String(data.value));
        } else {
          for (const attr of ['role', 'aria-label', 'aria-valuemin', 'aria-valuemax', 'aria-valuenow']) dial.removeAttribute(attr);
        }
        const period = data.period || data.as_of || '';
        node.querySelector('.pulse-date').textContent = available
          ? `${period} 기준${data.rating ? ` · ${data.rating}` : ''}${data.status === 'stale' ? ' · 갱신 실패' : ''}`
          : (data.message || '조회 불가');
        node.title = data.description || data.message || '';
        node.dataset.status = data.status;
      } catch {
        node.dataset.status = 'stale';
        const label = node.querySelector('.pulse-date');
        label.textContent = label.textContent.includes('기준')
          ? label.textContent.replace(/ · 갱신 실패$/, '') + ' · 갱신 실패' : '조회 불가';
        node.title = '연결 실패. 표시된 값이 있으면 이전 기준일의 자료입니다.';
      }
    }));
    loading = false;
  }
  refresh();
  setInterval(refresh, 300000);
  document.addEventListener('visibilitychange', refresh);
})();

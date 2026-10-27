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
          // 버핏지수는 최대 25초인 FRED 요청 두 건을 순서대로 기다린다.
          signal: AbortSignal.timeout(node.dataset.indicator === 'buffett' ? 60000 : 30000), cache: 'no-store',
        });
        if (!response.ok) throw new Error('request');
        const data = await response.json();
        const isSentiment = node.dataset.indicator === 'fear_greed';
        const available = Number.isFinite(data.value) && ['ok', 'stale'].includes(data.status)
          && (!isSentiment || (data.value >= 0 && data.value <= 100));
        node.querySelector('.pulse-value').textContent = available
          ? data.value.toLocaleString('ko-KR', {maximumFractionDigits: isSentiment ? 0 : 2}) : '—';
        const dial = node.querySelector(isSentiment ? '.sentiment-position' : '.pulse-dial');
        if (isSentiment) {
          const marker = node.querySelector('.sentiment-marker');
          marker.hidden = !available;
          node.querySelector('.sentiment-score').hidden = !available;
          // 공급자 분류를 강조하되 조회 불가일 때 이전 강조를 남기지 않는다.
          document.querySelectorAll('.sentiment-labels [data-rating], .sentiment-ranges [data-rating]').forEach(item => {
            const current = available && item.dataset.rating === data.rating;
            item.classList.toggle('is-current', current);
            if (current) item.setAttribute('aria-current', 'true');
            else item.removeAttribute('aria-current');
            const tag = item.querySelector('.sentiment-current-tag');
            if (tag) tag.hidden = !current;
          });
          document.getElementById('sentiment-current').textContent = available
            ? `${data.value.toLocaleString('ko-KR', {maximumFractionDigits: 2})}점 · ${data.rating || '분류 미제공'} · ${data.period || data.as_of || ''} 기준${data.status === 'stale' ? ' · 갱신 실패, 이전 자료' : ''}`
            : '현재 구간을 확인할 수 없습니다.';
          if (available) {
            node.querySelector('.sentiment-position').style.setProperty('--score-position', `${data.value}%`);
            // 색상은 공급자의 분류를 따른다. 미확인 분류를 임의로 단정하지 않는다.
            const colors = {'극도의 공포':'#e5807b', '공포':'#eda663', '중립':'#a0a9ae', '탐욕':'#a3cc83', '극도의 탐욕':'#57bb92'};
            node.style.setProperty('--sentiment-color', colors[data.rating] || '#a0a9ae');
          }
        }
        // CNN 점수에만 접근성 범위를 부여한다. 위치는 아래 구간 바에 표시한다.
        if (isSentiment && available) {
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
          ? `${period} 기준${!isSentiment && data.rating ? ` · ${data.rating}` : ''}${data.status === 'stale' ? ' · 갱신 실패' : ''}`
          : (data.message || '조회 불가');
        node.title = data.description || data.message || '';
        node.dataset.status = data.status;
      } catch {
        node.dataset.status = 'stale';
        const label = node.querySelector('.pulse-date');
        label.textContent = label.textContent.includes('기준')
          ? label.textContent.replace(/ · 갱신 실패$/, '') + ' · 갱신 실패' : '조회 불가';
        node.title = '연결 실패. 표시된 값이 있으면 이전 기준일의 자료입니다.';
        if (node.dataset.indicator === 'fear_greed') {
          const current = document.getElementById('sentiment-current');
          if (!current.textContent.includes('갱신 실패')) current.textContent += ' · 갱신 실패, 이전 자료';
        }
      }
    }));
    loading = false;
  }
  refresh();
  setInterval(refresh, 300000);
  document.addEventListener('visibilitychange', refresh);
})();

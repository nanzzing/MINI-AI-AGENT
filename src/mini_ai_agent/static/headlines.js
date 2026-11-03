/* 공통 뉴스는 대화 DOM과 분리해 로그인 상태와 무관하게 유지한다. */
(() => {
  const track = document.getElementById('headline-track');
  const status = document.getElementById('headline-status');
  const button = document.getElementById('headline-pause');
  let paused = false, signature = '', loading = false;
  button.addEventListener('click', () => {
    paused = !paused;
    button.closest('.headline-strip').classList.toggle('is-paused', paused);
    button.setAttribute('aria-pressed', String(paused));
    button.title = paused ? '뉴스 흐름 재생' : '뉴스 흐름 일시정지';
    button.setAttribute('aria-label', button.title);
    button.replaceChildren();
    const icon = document.createElement('i'); icon.dataset.lucide = paused ? 'play' : 'pause';
    button.append(icon); window.lucide?.createIcons();
  });
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const response = await fetch('/api/headlines', {signal:AbortSignal.timeout(45000)});
      if (!response.ok) throw new Error('headlines');
      const data = await response.json();
      const next = JSON.stringify(data.items);
      // 키보드로 읽는 도중 링크를 교체하지 않는다. 다음 주기에 다시 갱신한다.
      if (next !== signature && !track.contains(document.activeElement)) {
        const group = document.createElement('div'); group.className = 'headline-group';
        for (const item of data.items) {
          const url = new URL(item.url);
          if (url.protocol !== 'https:' || url.hostname !== 'news.google.com') continue;
          const link = document.createElement('a'); link.href = url.href;
          link.target = '_blank'; link.rel = 'noopener noreferrer';
          link.title = `${item.title} · ${item.publisher} · ${new Date(item.published_at).toLocaleString('ko-KR')} (새 탭)`;
          const category = document.createElement('em'); category.textContent = item.group;
          const publisher = document.createElement('small'); publisher.textContent = item.publisher;
          link.append(category, document.createTextNode(item.title), publisher); group.append(link);
        }
        const copy = group.cloneNode(true); copy.setAttribute('aria-hidden', 'true');
        copy.querySelectorAll('a').forEach(a => a.tabIndex = -1);
        track.replaceChildren(group, copy); signature = next;
        track.style.setProperty('--headline-duration', `${Math.max(40, group.getBoundingClientRect().width / 35)}s`);
      }
      const stamp = data.fetched_at ? new Date(data.fetched_at).toLocaleTimeString('ko-KR', {hour:'2-digit', minute:'2-digit'}) : '';
      status.textContent = data.items.length ? `${data.stale ? '일부 갱신 실패 · ' : ''}${stamp} 갱신` : '확인된 헤드라인 없음';
    } catch {
      status.textContent = track.querySelector('a') ? '갱신 실패 · 이전 목록 유지' : '뉴스를 불러오지 못했어요';
    } finally { loading = false; }
  }
  refresh(); setInterval(refresh, 900000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();

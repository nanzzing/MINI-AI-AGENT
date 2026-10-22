// 실제 HTTP 청크 스트리밍으로 UI를 검증한다. 모델/주가/뉴스 호출은 전부 모의 응답이다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const base = process.env.MINI_AGENT_URL || 'http://127.0.0.1:8766';
  const { research_focus: focus } = await (await fetch(`${base}/api/status`)).json();
  const root = path.join(__dirname, '../src/mini_ai_agent/static');
  const files = new Set(['index.html', 'app.js', 'app.css', 'auth.js', 'lucide.js', 'market.js',
    'market.css', 'charts.js', 'indicators.js', 'drawings.js']);
  const pending = [];
  let arrived;
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const json = data => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)); };
    if (url.pathname === '/api/status') return json({ provider: 'api', configured: true,
      model: 'progress-fixture', research_focus: focus });
    if (url.pathname === '/api/discovery') return json({ news: [], active: [] });
    if (url.pathname === '/api/market') return json({ status: 'not_found', matches: [] });
    if (url.pathname === '/api/chat') {
      assert(req.headers.accept.includes('application/x-ndjson'));
      req.resume();
      res.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8' });
      res.flushHeaders();
      pending.push(res);
      arrived?.();
      arrived = null;
      return;
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!files.has(name)) { res.writeHead(404); res.end(); return; }
    const mime = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html';
    res.writeHead(200, { 'Content-Type': mime + '; charset=utf-8' });
    res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' });
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 1000 } });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.waitForFunction(() => document.querySelector('.connection').textContent.includes('progress-fixture'));
      const submit = async () => {
        const received = new Promise(resolve => { arrived = resolve; });
        await page.locator('#message').fill('삼성전자 단기');
        await page.locator('#chat-form').evaluate(form => form.requestSubmit());
        await received;
        return pending.shift();
      };
      const write = (res, event) => res.write(JSON.stringify(event) + '\n');
      const steps = [];
      const progress = (res, step) => { steps.push(step); write(res, { type: 'progress', step }); };
      const response = await submit();
      progress(response, { event: '모델 호출', round: 1, detail: '도구 선택 대기', elapsed_ms: 0 });
      await page.getByRole('status').filter({ hasText: '모델 1차 응답 대기 중' }).waitFor();
      await page.waitForFunction(() => Number(document.querySelector('.pending-stage-elapsed').textContent.match(/\d+/)[0]) >= 2);
      assert(parseInt(await page.locator('.pending-elapsed').innerText(), 10) >= 2);
      progress(response, { event: '모델 응답 수신', round: 1, duration_ms: 1200, detail: '도구 요청 수신' });
      const tool = { event: '모델 → 파이썬', round: 2, tool: 'get_financials', detail: '재무 조회 요청' };
      steps.push(tool);
      // JSON 줄뿐 아니라 한국어 UTF-8 글자도 서로 다른 청크로 분리한다.
      const bytes = Buffer.from(JSON.stringify({ type: 'progress', step: tool }) + '\n');
      const split = bytes.indexOf(Buffer.from('재무')) + 1;
      response.write(bytes.subarray(0, split));
      await new Promise(resolve => setTimeout(resolve, 25));
      response.write(bytes.subarray(split));
      await page.getByRole('status').filter({ hasText: '재무 조회 중' }).waitFor();
      assert.equal(await page.locator('.pending-stage-elapsed').innerText(), '현재 단계 0초');
      await page.locator('.pending-trace summary').click();
      assert.match(await page.locator('.pending-trace').innerText(), /1.2초 소요/);
      assert(await page.locator('.send').isDisabled());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert(await page.locator('.pending-status').evaluate(node => node.scrollWidth <= node.clientWidth));
      await page.locator('.chat-panel').screenshot({ path: path.join(os.tmpdir(), `mini-progress-${width}.png`) });

      progress(response, { event: '파이썬 실행 완료', round: 2, tool: 'get_financials', duration_ms: 2500, detail: '자료 반환' });
      progress(response, { event: '모델 호출', round: 3, detail: '분석 응답 대기' });
      await page.getByRole('status').filter({ hasText: '모델 3차 응답 대기 중' }).waitFor();
      const limitations = '수익률 기반 변동성만 제공하며 ATR 계산값은 미제공입니다.';
      const coverage = focus['단기'].map(criterion => ({ criterion, status: 'no_data', reason: '자료 부족',
        sources: [], available_sources: [], claims: [], limitations: criterion === '변동성 및 ATR' ? limitations : '' }));
      write(response, { type: 'result', data: { reply: '검증된 최종 답변', horizon: '단기', focus: focus['단기'],
        research: {}, tool_calls: [], agent_steps: steps, coverage } });
      response.end();
      await page.locator('.message.pending').waitFor({ state: 'detached' });
      assert.match(await page.locator('#messages').innerText(), /검증된 최종 답변/);
      assert.match(await page.locator('#messages').innerText(), /2.5초 소요/);
      await page.getByText('변동성 및 ATR', { exact: true }).click();
      assert.match(await page.locator('.criterion-limitation').innerText(), /ATR 계산값은 미제공/);

      // 헤더 전송 후의 오류와 최종 이벤트 없는 연결 종료 모두 마지막 단계를 보존한다.
      for (const terminal of ['error', 'disconnect', 'malformed']) {
        const failing = await submit();
        write(failing, { type: 'progress', step: { event: '모델 호출', round: 4, detail: '마지막 대기 단계' } });
        await page.getByRole('status').filter({ hasText: '모델 4차 응답 대기 중' }).waitFor();
        if (terminal === 'error') write(failing, { type: 'error', error: '모의 서버 오류' });
        if (terminal === 'malformed') failing.write('{invalid json}\n');
        failing.end();
        await page.locator('.message.pending').waitFor({ state: 'detached' });
        const failure = await page.locator('.message.error').last().innerText();
        assert.match(failure, /4차 · 모델 호출/);
        assert.match(failure, terminal === 'error' ? /모의 서버 오류/ : terminal === 'disconnect' ? /최종 답변을 받기 전에/ : /해석하지 못했어요/);
        assert.equal(await page.locator('.send').isDisabled(), false);
        assert.equal(await page.locator('#composer-status').innerText(), '종목 · 투자 관점 · 궁금한 점');
      }
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: live stages before final answer, durations, split UTF-8/JSON, limits, errors and disconnect recovery`);
      await page.close();
    }
  } finally {
    for (const response of pending) response.end();
    await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

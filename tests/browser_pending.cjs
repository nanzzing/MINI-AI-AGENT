// 실제 모델 대신 지연된 HTTP 응답으로 대기 표시, 경과 시간, 타이머 정리를 확인한다.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const base = process.env.MINI_AGENT_URL || 'http://127.0.0.1:8765';
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' });
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 },
        reducedMotion: width === 390 ? 'reduce' : 'no-preference' });
      const requests = [];
      let requestArrived;
      await context.route('**/api/status', route => route.fulfill({ json: {
        provider: 'api', configured: true, model: 'fixture-model', research_focus: {},
      } }));
      await context.route('**/api/discovery', route => route.fulfill({ json: { news: [], active: [] } }));
      await context.route('**/api/market?**', route => route.fulfill({ json: { status: 'not_found', matches: [] } }));
      await context.route('**/api/chat', route => {
        requests.push(route);
        requestArrived?.();
        requestArrived = null;
      });
      // 인터벌 생성/해제를 계수해 화면에서만 숨긴 채 타이머가 남는 회귀도 잡는다.
      await context.addInitScript(() => {
        const set = window.setInterval.bind(window), clear = window.clearInterval.bind(window);
        window.testIntervals = new Set();
        window.setInterval = (...args) => {
          const id = set(...args);
          window.testIntervals.add(id);
          return id;
        };
        window.clearInterval = id => { window.testIntervals.delete(id); clear(id); };
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.waitForFunction(() => document.querySelector('.connection').textContent.includes('fixture-model'));
      const baseline = await page.evaluate(() => window.testIntervals.size);
      const submit = async text => {
        const arrived = new Promise(resolve => { requestArrived = resolve; });
        await page.locator('#message').fill(text);
        await page.locator('#chat-form').evaluate(form => form.requestSubmit());
        await page.locator('.message.pending').waitFor();
        await arrived;
      };
      const ready = async () => {
        await page.locator('.message.pending').waitFor({ state: 'detached' });
        assert.equal(await page.locator('#composer-status').innerText(), '종목 · 투자 관점 · 궁금한 점');
        assert.equal(await page.evaluate(() => window.testIntervals.size), baseline);
        assert.equal(await page.locator('.send').isDisabled(), false);
      };
      await submit('삼성전자 장기');
      assert.match(await page.locator('.pending-status').innerText(), /자료를 조사하고 답변을 준비하고 있어요/);
      await page.waitForFunction(() => parseInt(document.querySelector('.pending-elapsed').textContent, 10) >= 2);
      assert.match(await page.locator('#composer-status').innerText(), /조사·답변 준비 중 · \d+초 경과/);
      assert(await page.locator('.send').isDisabled());
      // 대기 중 재전송은 새 모델 요청을 만들거나 타이머를 중복 생성하지 않는다.
      await page.locator('#chat-form').evaluate(form => form.requestSubmit());
      assert.equal(requests.length, 1);
      assert.equal(await page.locator('.message.pending').count(), 1);
      assert.equal(await page.evaluate(() => window.testIntervals.size), baseline + 1);
      assert.equal(await page.locator('.pending-spinner').evaluate(node => getComputedStyle(node).animationName),
        width === 390 ? 'none' : 'pending-spin');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert(await page.locator('.pending-status').evaluate(node => node.scrollWidth <= node.clientWidth));
      await page.screenshot({ path: path.join(os.tmpdir(), `mini-pending-${width}.png`), fullPage: true });
      const reply = { reply: '조사 완료', research: {}, focus: [], tool_calls: [] };
      await requests.shift().fulfill({ json: reply });
      await ready();
      assert.match(await page.locator('.message-duration').innerText(), /\d+초 소요/);
      assert((await page.locator('#messages').innerText()).includes('조사 완료'));

      // 다음 질문은 시간을 새로 세며, HTTP 오류 뒤에도 입력과 상태가 복구된다.
      await submit('최근 기사도 알려줘');
      assert.equal(await page.locator('.pending-elapsed').innerText(), '0초 경과');
      await requests.shift().fulfill({ status: 503, json: { error: '테스트 연결 오류' } });
      await ready();
      assert.equal(await page.locator('.message.error').count(), 1);

      // 네트워크 단절은 JSON 오류 응답과 별개의 실패 경로다.
      await submit('다시 조사해줘');
      await requests.shift().abort('failed');
      await ready();
      assert.equal(await page.locator('.message.error').count(), 2);

      // 대화 초기화도 정리 경로를 공유하지만 소요 시간 배지는 붙이지 않는다.
      const resetArrived = new Promise(resolve => { requestArrived = resolve; });
      await page.evaluate(() => { void send('/reset', true); });
      await page.locator('.message.pending').waitFor();
      await resetArrived;
      assert.match(await page.locator('.pending-status').innerText(), /대화를 초기화하고 있어요/);
      await requests.shift().fulfill({ json: { ...reply, reply: '새로운 조사를 시작할게요.' } });
      await ready();
      assert.equal(await page.locator('.message-duration').count(), 0);
      assert.equal(await page.locator('.message').count(), 1);
      await context.close();
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: pending timer, duplicate prevention, completion, HTTP/network errors, reduced motion, no overflow`);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

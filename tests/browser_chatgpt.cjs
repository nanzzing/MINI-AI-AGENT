// OAuth/모델 호출은 모두 대체한다. 실제 계정 로그인이나 사용량 소비 없이 UI를 검사한다.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const base = process.env.MINI_AGENT_URL || 'http://127.0.0.1:8765';
  const realStatus = await (await fetch(`${base}/api/status`)).json();
  assert.equal(realStatus.provider, 'chatgpt');
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' });
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const state = { provider: 'chatgpt', configured: false, connected: false, plan_enabled: false,
        profiles: [], models: [], model: '', csrf: 'fixture-csrf', login_pending: false, research_focus: realStatus.research_focus };
      const calls = [];
      await context.route('**/api/status', route => route.fulfill({ json: state }));
      await context.route('**/api/discovery', route => route.fulfill({ json: { news: [], active: [] } }));
      await context.route('**/api/market?**', route => route.fulfill({ json: { status: 'not_found', matches: [] } }));
      await context.route('https://auth.openai.com/**', async route => {
        Object.assign(state, { connected: true, plan_enabled: true, login_pending: false,
          active_profile: 'p1', profiles: [{ id: 'p1', label: 'student@example.com', connected: true }] });
        await route.fulfill({ contentType: 'text/html', body: '<title>Mock authorization</title>Mock sign-in complete' });
      });
      await context.route('**/api/auth/**', async route => {
        const request = route.request(), body = request.postDataJSON();
        assert.equal(request.headers()['x-mini-csrf'], 'fixture-csrf');
        const endpoint = new URL(request.url()).pathname;
        calls.push({ endpoint, body });
        if (endpoint.endsWith('/login')) {
          state.login_pending = true;
          await route.fulfill({ json: { authorization_url: 'https://auth.openai.com/api/accounts/authorize?state=mock' } });
          return;
        }
        if (endpoint.endsWith('/models')) Object.assign(state, { configured: true, model: 'model-a',
          models: [{ slug: 'model-a', display_name: 'Research model A' }, { slug: 'model-b', display_name: 'Research model B' }] });
        if (endpoint.endsWith('/select')) Object.assign(state, body.model ? { model: body.model } : { active_profile: body.profile_id });
        if (endpoint.endsWith('/logout')) Object.assign(state, { configured: false, connected: false, plan_enabled: false,
          models: [], model: '', profiles: [{ id: 'p1', label: 'student@example.com', connected: false }] });
        await route.fulfill({ json: endpoint.endsWith('/logout') ? { message: '로그아웃했어요.' } : state });
      });
      await context.route('**/api/chat', route => {
        assert.equal(route.request().headers()['x-mini-csrf'], 'fixture-csrf');
        return route.fulfill({ json: { reply: '새로운 조사를 시작할게요.', research: {}, focus: [], tool_calls: [] } });
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.locator('#chatgpt-controls').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#auth-login').innerText(), 'Continue with ChatGPT');
      assert(await page.locator('#auth-logout').isDisabled());
      await page.screenshot({ path: path.join(os.tmpdir(), `mini-chatgpt-signedout-${width}.png`), fullPage: true });
      const popup = page.waitForEvent('popup');
      await page.locator('#auth-login').click();
      await popup;
      await page.locator('#auth-welcome').waitFor({ state: 'visible', timeout: 10000 });
      await page.locator('#auth-welcome-close').click();
      await page.waitForFunction(() => !document.getElementById('auth-model').disabled);
      await page.locator('#auth-model').selectOption('model-b');
      await page.waitForFunction(() => document.querySelector('.connection').textContent.includes('model-b'));
      assert(calls.some(call => call.endpoint.endsWith('/select') && call.body.model === 'model-b'));
      await page.waitForFunction(() => !document.getElementById('auth-login').disabled);
      await page.screenshot({ path: path.join(os.tmpdir(), `mini-chatgpt-connected-${width}.png`), fullPage: true });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      // 다른 계정을 추가할 때 현재 profile ID를 실수로 다시 보내지 않는지 확인한다.
      await page.locator('#auth-profile').selectOption('');
      await page.locator('#auth-login').click();
      await page.waitForFunction(() => document.getElementById('auth-welcome').open);
      assert.equal(calls.filter(call => call.endpoint.endsWith('/login')).at(-1).body.profile_id, null);
      await page.locator('#auth-welcome-close').click();
      await page.waitForFunction(() => !document.getElementById('auth-logout').disabled);
      await page.locator('#auth-logout').click();
      await page.waitForFunction(() => document.getElementById('auth-state').textContent === '로그인 필요');
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: login, consent notice, models, account selection, logout, CSRF, no overflow`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

// Mock HTTP responses: verify selection and follow-ups without paid model calls.
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      const errors = [], requests = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.route('**/api/**', route => {
        const pathname = new URL(route.request().url()).pathname;
        if (pathname === '/api/chat') {
          requests.push(route.request().postDataJSON());
          return route.fulfill({json:{reply:'수요 전망 테스트', horizon:null, focus:[], coverage:[], research:{response_mode:'industry'}, tool_calls:[], agent_steps:[]}});
        }
        return route.fulfill({json:{configured:true, provider:'openai', model:'test', status:'unavailable', research_focus:{}, stocks:[], active:[]}});
      });
      await page.goto(process.env.MINI_AGENT_URL || 'http://127.0.0.1:8766/');
      assert.equal(await page.locator('[data-horizon="자동"]').getAttribute('aria-pressed'), 'true');
      await page.locator('[data-horizon="장기"]').click();
      assert.equal(requests.length, 0);
      await page.locator('[data-horizon="자동"]').click();
      await page.locator('#message').fill('반도체 수요는 계속 갈까?');
      await page.locator('#message').press('Enter');
      await page.waitForFunction(() => !busy);
      assert.equal(requests[0].message, '반도체 수요는 계속 갈까?');
      assert.equal(requests[0].selection, '자동');
      assert.equal(await page.locator('#criteria-title').textContent(), '이번 답변의 조회 근거');
      await page.locator('#message').fill('왜 그렇게 생각해?');
      await page.locator('#message').press('Enter');
      await page.waitForFunction(() => !busy);
      assert.equal(requests[1].message, '왜 그렇게 생각해?');
      await page.locator('[data-horizon="장기"]').click();
      await page.locator('#message').fill('삼전 어때?');
      await page.locator('#message').press('Enter');
      await page.waitForFunction(() => !busy);
      assert.equal(requests[2].message, '삼전 어때? (장기 관점)');
      assert.deepEqual(errors, []);
      await page.screenshot({path:require('node:path').join(require('node:os').tmpdir(), `mini-auto-${width}.png`),fullPage:true});
      await page.close();
    }
    console.log('PASS auto selector, follow-up, explicit horizon, desktop/mobile');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});

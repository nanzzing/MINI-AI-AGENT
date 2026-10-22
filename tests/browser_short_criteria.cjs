// 기준표는 실행 서버에서 읽고 채팅만 대체한다. 실제 모델 호출/토큰 사용은 없다.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const base = process.env.MINI_AGENT_URL || 'http://127.0.0.1:8766';
  const {research_focus: focus} = await (await fetch(`${base}/api/status`)).json();
  const expected = ['RSI', 'RSI 상승다이버전스', 'RSI 하락다이버전스',
                    'MACD', 'MACD 상승다이버전스', 'MACD 하락다이버전스'];
  assert.deepEqual(focus['단기'].slice(1, 7), expected);
  assert.equal(focus['단기'].length, 16);
  assert.equal(focus['중장기'].length, 12);
  assert.equal(focus['장기'].length, 20);
  const browser = await chromium.launch({headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport: {width, height: 1000}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/api/discovery', route => route.fulfill({json: {news: [], active: []}}));
      await page.route('**/api/chat', route => {
        const horizon = route.request().postDataJSON().message.replace('중기', '중장기');
        return route.fulfill({json: {
        reply: '조사 기준 테스트', horizon, focus: focus[horizon], research: {},
        tool_calls: [], agent_steps: [], coverage: [],
      }});
      });
      await page.goto(base);
      await page.locator('[data-horizon="단기"]').click();
      await page.waitForFunction(() => document.querySelectorAll('#criteria > li').length === 16);
      const text = await page.locator('#criteria').innerText();
      for (const label of expected) assert(text.includes(label));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.locator('#criteria').screenshot({path: path.join(os.tmpdir(), `mini-agent-divergence-${width}.png`)});
      for (const [label, horizon] of [['중기', '중장기'], ['장기', '장기']]) {
        await page.locator(`[data-horizon="${label}"]`).click();
        await page.waitForFunction(count => document.querySelectorAll('#criteria > li').length === count, focus[horizon].length);
        assert.deepEqual(await page.locator('.criterion-label').allTextContents(), focus[horizon]);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      }
      await page.locator('#criteria').screenshot({path: path.join(os.tmpdir(), `mini-agent-long-valuation-${width}.png`)});
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}px: 16/12/20 criteria, six distinct RSI/MACD entries, no overflow`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

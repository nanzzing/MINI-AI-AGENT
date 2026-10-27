// DART 화면 계약 테스트. 공시/모델 API는 호출하지 않는다.
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.route('**/api/**', r => r.fulfill({json:{status:'unavailable', stocks:[], bars:[], news:[], active:[]}}));
      await page.goto(process.env.MINI_AGENT_URL || 'http://127.0.0.1:8765/');
      await page.waitForFunction(() => typeof marketView !== 'undefined');
      await page.evaluate(() => marketView.research({financials:{
        source:'OpenDART', currency:'KRW', note:'DART 연결 우선', missing:['요청 5기간, 제공 1기간'],
        statements:{yearly:{income:[{period_end:'2025-12-31',scope:'CFS',receipt_no:'20260316000001',values:{'Total Revenue':123456}}]}}
      }}));
      await page.locator('#financial-result summary').click();
      assert.match(await page.locator('#financial-result').innerText(), /OpenDART/);
      assert.match(await page.locator('#financial-result').innerText(), /연결/);
      assert.match(await page.locator('#financial-result').innerText(), /123,456/);
      assert.match(await page.locator('#financial-result a').getAttribute('href'), /rcpNo=20260316000001$/);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.locator('#financial-result').screenshot({path:path.join(os.tmpdir(),`mini-dart-${width}.png`)});
      await page.evaluate(() => marketView.research({financials:{
        source:'OpenDART', currency:'KRW', status:'unavailable', message:'DART_API_KEY 미설정', statements:{}
      }}));
      assert.match(await page.locator('#financial-result').innerText(), /DART_API_KEY 미설정/);
      assert.equal(await page.locator('#financial-result table').count(), 0);
      assert.deepEqual(errors, []);
      console.log(`PASS DART ${width}: source, scope, link, table, missing key`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode=1; });

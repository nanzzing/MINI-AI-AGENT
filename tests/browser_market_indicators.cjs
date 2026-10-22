const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440,390,320]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      let mode = 'ok', chats = 0;
      page.on('request', r => { if (r.url().endsWith('/api/chat')) chats++; });
      await page.route('**/api/discovery', r => r.fulfill({json:{news:[],active:[]}}));
      await page.route('**/api/market-indicator?*', async r => {
        const name = new URL(r.request().url()).searchParams.get('name');
        if (mode === 'error') return r.abort();
        await r.fulfill({json: {status:mode, value:name==='vix'?16.68:name==='buffett'?255.05:0,
          unit:name==='buffett'?'%':name==='fear_greed'?'/100':'', period:name==='buffett'?'2026 Q2':'2026-10-21',
          rating:name==='fear_greed'?'극도의 공포':''}});
      });
      await page.goto('http://127.0.0.1:8766/');
      await page.waitForFunction(() => document.querySelector('[data-indicator="fear_greed"] .pulse-value').textContent==='0');
      assert.equal(await page.locator('[role="meter"]').count(),1);
      assert.equal(await page.locator('[data-indicator="fear_greed"] [role="meter"]').getAttribute('aria-valuenow'),'0');
      assert.equal(await page.locator('.market-pulse article').count(),3);
      assert(await page.evaluate(()=> document.documentElement.scrollWidth<=innerWidth));
      for (const name of ['vix', 'buffett']) {
        const trigger = page.locator(`[data-help="${name}-help"]`);
        await trigger.click();
        const dialog = page.locator(`#${name}-help`);
        assert(await dialog.isVisible());
        const box = await dialog.boundingBox();
        assert(box.x >= 0 && box.x + box.width <= width);
        assert(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth));
        await dialog.screenshot({path:path.join(os.tmpdir(),`mini-${name}-help-${width}.png`)});
        await page.keyboard.press('Escape');
        assert(!(await dialog.isVisible()));
        assert(await trigger.evaluate(el => document.activeElement === el));
        await trigger.click();
        await dialog.getByRole('button', {name:'도움말 닫기'}).click();
        assert(!(await dialog.isVisible()));
      }
      await page.evaluate(()=>window.scrollTo(0,400));
      assert((await page.locator('.market-pulse').boundingBox()).y >= -1);
      await page.locator('.market-pulse summary').click();
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.locator('.market-pulse summary').click();
      await page.locator('.market-pulse').screenshot({path:path.join(os.tmpdir(),`mini-market-pulse-${width}.png`)});
      mode='error';
      await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
      await page.waitForFunction(()=>document.querySelector('.pulse-date').textContent.includes('갱신 실패'));
      mode='unavailable';
      await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
      await page.waitForFunction(()=>document.querySelector('.pulse-date').textContent==='조회 불가');
      assert.equal(await page.locator('[role="meter"]').count(),0);
      assert.equal(chats,0);
      console.log(`PASS ${width}: sticky, values, failure, overflow, no LLM calls`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});

const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport:{width,height:1000}, reducedMotion:'no-preference'});
      await page.route('**/api/**', route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/headlines') return route.continue();
        if (path === '/api/account') return route.fulfill({json:{enabled:false,user:null}});
        return route.fulfill({json:{configured:true,provider:'openai', status:'unavailable', research_focus:{}, stocks:[], news:[], active:[]}});
      });
      await page.goto('http://127.0.0.1:8770/');
      const links = page.locator('.headline-group:not([aria-hidden]) a');
      await links.first().waitFor({timeout:45000});
      assert.ok(await links.count() <= 12);
      assert.equal(await links.first().getAttribute('target'), '_blank');
      const track = page.locator('#headline-track');
      const before = await track.evaluate(e => getComputedStyle(e).transform);
      await page.waitForTimeout(300);
      assert.notEqual(await track.evaluate(e => getComputedStyle(e).transform), before);
      await page.locator('#headline-pause').click();
      assert.equal(await page.locator('#headline-pause').getAttribute('aria-pressed'), 'true');
      assert.equal(await track.evaluate(e => getComputedStyle(e).animationPlayState), 'paused');
      await page.evaluate(() => window.researchHistory.clear());
      assert.ok(await links.count() > 0);
      assert.equal(await page.locator('#discovery').count(), 1);
      assert.ok(await page.locator('.headline-strip').evaluate(e => e.getBoundingClientRect().right <= innerWidth));
      await page.screenshot({path:path.join(os.tmpdir(), `mini-headlines-${width}.png`)});
      await page.emulateMedia({reducedMotion:'reduce'});
      assert.equal(await track.evaluate(e => getComputedStyle(e).animationName), 'none');
      await page.close();
    }
    console.log('Desktop/mobile headlines: live feed, animation, pause, reset, reduced motion passed');
  } finally { await browser.close(); }
})();

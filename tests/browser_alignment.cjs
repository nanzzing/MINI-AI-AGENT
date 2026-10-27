// Network-free chart hover regression, using deterministic increasing/flat prices.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport:{width, height:1000}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      let flat = false;
      await page.route('**/api/**', route => {
        if (new URL(route.request().url()).pathname !== '/api/market')
          return route.fulfill({json:{status:'unavailable',stocks:[],active:[]}});
        const bars = Array.from({length:320}, (_, i) => {
          const close = flat ? 100 : 100 + i;
          return {time:new Date(Date.UTC(2025,0,1+i)).toISOString().slice(0,10),
            open:close-1, high:close+2, low:close-2, close, volume:1000};
        });
        return route.fulfill({json:{stock:{name:'테스트',code:'123456',exchange:'KOSPI'},
          data:{bars,currency:'KRW'},policy:{annual_years:5,quarters:8}}});
      });
      await page.goto(process.env.MINI_AGENT_URL || 'http://127.0.0.1:8765/');
      await page.waitForFunction(() => typeof marketView !== 'undefined');
      await page.evaluate(() => marketView.load({query:'test'}));
      const chart = page.locator('#market-chart');
      const badge = page.locator('.ma-alignment-badge');
      async function hover() {
        await chart.scrollIntoViewIfNeeded();
        if (width < 500) await page.evaluate(() => window.scrollBy(0, -180));
        await page.waitForTimeout(200);
        const box = await chart.boundingBox();
        await page.mouse.move(box.x + box.width * .45, box.y + 100);
        await page.waitForTimeout(100);
      }
      await hover();
      assert(await badge.isVisible());
      assert.match(await badge.innerText(), /정배열!/);
      const bounds = await chart.boundingBox(), tag = await badge.boundingBox();
      assert(tag.x >= bounds.x && tag.x + tag.width <= bounds.x + bounds.width);
      await chart.screenshot({path:path.join(os.tmpdir(), `mini-alignment-${width}.png`)});
      await page.mouse.move(0,0);
      await page.waitForTimeout(100);
      assert(await badge.isHidden());
      await page.locator('[data-range="MONTH"]').click();
      await hover();
      assert(await badge.isHidden());
      await page.locator('[data-range="1Y"]').click();
      await page.getByRole('button',{name:'차트 크게 보기',exact:true}).click();
      // Latest portion has all four averages even in the one-year expanded view.
      await chart.scrollIntoViewIfNeeded();
      await page.waitForTimeout(200);
      let b = await chart.boundingBox();
      await page.mouse.move(b.x+(b.width-70)*.85,b.y+100);
      await page.waitForTimeout(100);
      assert(await badge.isVisible());
      flat = true;
      await page.evaluate(() => marketView.load({query:'flat'}));
      await hover();
      assert(await badge.isHidden());
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}: alignment, leave, monthly, expanded, equal averages`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});

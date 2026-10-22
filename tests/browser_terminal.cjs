// Theme regression: no model calls; real chart loading and responsive controls.
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[]; page.on('pageerror', e=>errors.push(e.message));
    await page.route('**/api/discovery', r=>r.fulfill({json:{news:[],active:[],fetched_at:new Date().toISOString()}}));
    await page.goto('http://127.0.0.1:8766/');
    await page.locator('#stock-query').fill('삼전');
    await page.locator('#stock-search').evaluate(f=>f.requestSubmit());
    await page.waitForFunction(()=>document.querySelector('#chart-legend').textContent.includes('종가'),null,{timeout:90000});
    await page.locator('#indicator-select').selectOption('rsi');
    const research = await page.locator('.research-panel').boundingBox();
    const chat = await page.locator('.chat-panel').boundingBox();
    assert(research.x < chat.x);
    assert(await page.locator('#market-chart canvas').first().evaluate(c=>{
      const a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      for(let i=0;i<a.length;i+=4) if(a[i]>80 && a[i]<150 && a[i+1]>210 && a[i+2]>150)return true;
      return false;
    }));
    for(const width of [1440,1024,760,390,320]) {
      await page.setViewportSize({width,height:1100});
      await page.waitForTimeout(150);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.screenshot({path:path.join(os.tmpdir(),`mini-terminal-${width}.png`),fullPage:true});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole('button',{name:'차트 크게 보기',exact:true}).click();
    assert(await page.getByRole('button',{name:'추세선: 시작점과 끝점 선택',exact:true}).isVisible());
    await page.getByRole('button',{name:'수평선: 가격 위치 선택',exact:true}).click();
    await page.locator('.drawing-overlay').click({position:{x:200,y:150}});
    assert.equal(await page.locator('.drawing-overlay line').count(),1);
    await page.keyboard.press('Control+z');
    assert.equal(await page.locator('.drawing-overlay line').count(),0);
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.chart-expanded').count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS dark chart pixels, desktop/mobile layout, indicator, expansion, drawing and undo');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

// 실행 중인 localhost:8765와 실제 시장 데이터가 필요한 브라우저 통합 테스트다.
// 서버/공급자 상태에 영향을 받으므로 순수 계산 테스트와 구분해서 실행한다.
const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto('http://127.0.0.1:8765');
    await page.locator('#stock-query').fill('삼전');
    await page.locator('#stock-search').evaluate(form=>form.requestSubmit());
    await page.waitForFunction(()=>document.querySelector('#chart-legend').textContent.includes('종가'),null,{timeout:90000});
    assert.match(await page.locator('#chart-title').textContent(),/005930/);
    assert.equal(await page.locator('#ma-legend label:visible').count(),4);
    assert.notEqual(await page.locator('#ma-legend output').first().textContent(),'—');
    await page.getByRole('checkbox',{name:'20일 이동평균선',exact:true}).uncheck();
    assert.equal(await page.getByRole('checkbox',{name:'20일 이동평균선',exact:true}).isChecked(),false);
    await page.getByRole('checkbox',{name:'20일 이동평균선',exact:true}).check();
    await page.locator('[data-range="MONTH"]').click();
    assert.equal(await page.locator('#ma-legend label:visible').count(),1);
    assert(await page.getByRole('checkbox',{name:'5개월 이동평균선',exact:true}).isVisible());
    assert.match(await page.locator('#chart-legend').textContent(),/미완성/);
    await page.locator('[data-range="1Y"]').click();
    await page.locator('#bollinger-toggle').check();
    await page.locator('#indicator-select').selectOption('rsi');
    assert(await page.locator('#indicator-panel').isVisible());
    assert.match(await page.locator('#indicator-values').textContent(),/RSI\(14\) [\d.]+/);
    await page.locator('#indicator-select').selectOption('signal');
    assert.match(await page.locator('#indicator-values').textContent(),/Signal\(9\) [-\d.]+/);
    await page.locator('#indicator-select').selectOption('macd');
    assert.match(await page.locator('#indicator-values').textContent(),/Histogram/);
    // Library canvas placement must not inherit the old demo canvas margin.
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      await page.waitForTimeout(150);
      const clipped=await page.evaluate(()=>['market-chart','indicator-chart'].flatMap(id=>{
        const root=document.getElementById(id),box=root.getBoundingClientRect();
        return [...root.querySelectorAll('canvas')].filter(canvas=>{
          const rect=canvas.getBoundingClientRect();
          return getComputedStyle(canvas).marginTop!=='0px'||rect.bottom>box.bottom+1||rect.top<box.top-1;
        }).map(()=>id);
      }));
      assert.deepEqual(clipped,[],`Clipped canvas at ${width}px`);
    }
    await page.setViewportSize({width:1440,height:1000});
    for(const id of ['indicator-chart','market-chart']){
      await page.locator(`#${id}`).hover({position:{x:120,y:65}});
      const priceDate=(await page.locator('#chart-legend').textContent()).match(/^\d{4}-\d{2}-\d{2}/)?.[0];
      assert(priceDate,`${id} hover should select a date`);
      assert((await page.locator('#indicator-values').textContent()).startsWith(priceDate));
    }
    await page.screenshot({path:'crosshair-preview.png',fullPage:true});
    // Actual colored candle pixels, not just a nonempty canvas background.
    assert(await page.locator('#market-chart').evaluate(root=>[...root.querySelectorAll('canvas')].some(c=>{
      const pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      for(let i=0;i<pixels.length;i+=4)if(pixels[i]<80&&pixels[i+1]>100&&pixels[i+1]<170&&pixels[i+2]<130)return true;
      return false;
    })));
    await page.screenshot({path:'market-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#market-chart').scrollIntoViewIfNeeded();
    assert(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)));
    await page.screenshot({path:'market-mobile.png'});
    await page.locator('#stock-query').fill('AAPL');
    await page.locator('#stock-search').evaluate(form=>form.requestSubmit());
    await page.waitForFunction(()=>document.querySelector('#chart-title').textContent.includes('AAPL')&&document.querySelector('#chart-legend').textContent.includes('종가'),null,{timeout:90000});
    assert.equal(await page.locator('#chart-currency').textContent(),'USD');
    await page.evaluate(()=>marketView.context({code:'AAPL',exchange:'NASDAQ',horizon:'장기',research:{
      news:{days:90,status:'ok',items:[{title:'News fixture',url:'https://example.com/',publisher:'Test',published_at:'2026-10-14'}]},
      events:{status:'not_requested'},
      financials:{source:'Test',currency:'USD',missing:['요청 5기간, 제공 1기간'],statements:{yearly:{income:[{period_end:'2025-12-31',values:{'Total Revenue':123}}]}}}
    }}));
    assert.match(await page.locator('#news-result').textContent(),/News fixture/);
    assert.match(await page.locator('#financial-result').textContent(),/123/);
    assert(await page.locator('#events-section').isHidden());
    assert.deepEqual(errors,[]);
    console.log('Live Samsung/AAPL charts, range controls, canvas pixels and mobile layout passed.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

// 모든 HTTP를 가로채 로컬 정적 파일과 가짜 응답으로 검사한다. 서버/키/실제 시세는 불필요하다.
// 모델 답변 품질이 아니라 도구 내역·체크리스트·도움말·반응형·안전한 텍스트 표시를 검증한다.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage();
    let turn = 0;
    const focus = {'단기':['RSI','MACD'], '중장기':['이동평균선 정배열','월봉 안착'], '장기':['사업 경쟁력','현금흐름']};
    // Serve local assets and stub all APIs so this test needs neither keys nor market data.
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'mini.test') return route.abort();
      if (url.pathname === '/api/status') return route.fulfill({json:{configured:true,model:'test',research_focus:focus}});
      if (url.pathname === '/api/market') return route.fulfill({json:{status:'not_found',matches:[]}});
      if (url.pathname === '/api/chat') {
        const message = route.request().postDataJSON().message;
        const match = message.match(/중장기|단기|중기|장기/);
        if (match) {
          const horizon = match[0] === '중기' ? '중장기' : match[0];
          await new Promise(resolve=>setTimeout(resolve,500));
          return route.fulfill({json:{reply:'관점 변경 완료',horizon,focus:focus[horizon],tool_calls:[],
            coverage:focus[horizon].map((criterion,index)=>({criterion,status:index?'not_declared':'declared',reason:'모델이 근거를 명시했습니다.',evidence:'',sources:index?[]:[{id:'c0s0',path:'technicals.rsi14',value:34.4,source:'Yahoo Finance'}],claims:index?[]:[{text:'관점 변경 완료',sources:[{id:'c0s0',path:'technicals.rsi14',value:34.4},{id:'c0s1',path:'technicals.macd_12_26_9',value:{line:-2}}]}]}))}});
        }
        if (message === '/reset') return route.fulfill({json:{reply:'초기화',horizon:null,focus:[],tool_calls:[]}});
        turn++;
        return route.fulfill({json:{reply:'테스트 답변',focus:[],tool_calls:turn === 1 ? [
          {name:'search_stock',arguments:{query:'<img src=x onerror=alert(1)>'},duration_ms:12,result:{stocks:[]}},
          {name:'get_financials',arguments:{},duration_ms:2,result:{error:'먼저 종목을 선택하세요.'}}
        ] : []}});
      }
      const name = url.pathname === '/' ? 'index.html' : path.basename(url.pathname);
      const body = await fs.readFile(path.join(__dirname,'../src/mini_ai_agent/static',name));
      return route.fulfill({body,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://mini.test/');
    await page.locator('#message').fill('테스트');
    await page.locator('#send').click();
    await page.locator('.tool-trace > summary').click();
    assert.match(await page.locator('.tool-trace').innerText(), /2회/);
    assert.equal(await page.locator('.tool-trace section').count(), 2);
    await page.locator('.tool-trace section details > summary').first().click();
    assert.match(await page.locator('.tool-trace pre').first().innerText(), /<img/);
    assert.equal(await page.locator('.tool-trace img').count(), 0);
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      await page.waitForTimeout(200);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth), false);
      await page.screenshot({path:path.join(require('node:os').tmpdir(),`mini-tool-trace-${width}.png`)});
    }
    await page.locator('#message').fill('다음 질문');
    await page.locator('#send').click();
    await page.locator('.tool-trace').nth(1).locator(':scope > summary').click();
    assert.match(await page.locator('.tool-trace').nth(1).innerText(), /도구 요청이 없습니다/);
    assert.equal(await page.locator('.tool-trace').count(), 2);
    for (const [query,horizon] of [['단기적으로 어때','단기'],['중장기로 어때','중장기'],['장기로 어때','장기']]) {
      await page.locator('#message').fill(query);
      await page.locator('#send').click();
      await page.waitForFunction(expected => document.querySelector('#criteria').textContent.includes(expected), focus[horizon][0]);
      assert.deepEqual(await page.locator('#criteria .criterion-label').allTextContents(),focus[horizon]);
      await page.waitForFunction(()=>!document.querySelector('#send').disabled);
      assert.deepEqual(await page.locator('#criteria .criterion-label').allTextContents(),focus[horizon]);
      assert.deepEqual(await page.locator('#criteria .criterion-status').allTextContents(),['O 모델이 사용 근거로 명시','X 제공됨 · 사용 근거 미명시']);
      await page.locator('#criteria summary').first().click();
      assert.equal(await page.locator('#criteria blockquote').first().innerText(),'관점 변경 완료');
      assert.equal(await page.getByText('이 분석·결론에 연결된 근거 2개',{exact:true}).count(),0);
      await page.getByText('현재 항목의 근거 자료',{exact:true}).click();
      assert.match(await page.locator('.criterion-source').first().innerText(),/34.4/);
      assert.equal(await page.locator('.criterion-source').count(),1);
      assert.doesNotMatch(await page.locator('.criterion-source').first().innerText(),/macd/);
    }
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:1000});
      await page.locator('#criteria').scrollIntoViewIfNeeded();
      await page.locator('#criteria-help summary').click();
      assert.equal(await page.locator('.criteria-help-content').isVisible(),true);
      assert.match(await page.locator('.criteria-help-content').innerText(),/사용 근거 목록/);
      const helpBox = await page.locator('.criteria-help-content').boundingBox();
      assert.ok(helpBox.x >= 0 && helpBox.x + helpBox.width <= width);
      await page.mouse.move(0,0);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#criteria-help').getAttribute('open'),null);
      await page.waitForTimeout(200);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(require('node:os').tmpdir(),`mini-coverage-${width}.png`)});
    }
    await page.locator('[data-horizon="단기"]').click();
    await page.waitForFunction(()=>document.querySelector('#criteria').textContent.includes('RSI'));
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    await page.setViewportSize({width:1440,height:900});
    await page.locator('#new-chat').click();
    await page.waitForFunction(()=>document.querySelector('#criteria-count').textContent==='—');
    console.log('Tool trace: order, arguments, errors, empty turn, safe text and responsive layouts passed.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});

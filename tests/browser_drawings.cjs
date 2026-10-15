// 실제 로컬 서버의 차트를 열고 확대·그리기·되돌리기·페이지 메모리 보관을 조작한다.
// Playwright는 사용자의 마우스/키보드 동작을 자동화하며 모델을 대신 판단하는 도구는 아니다.
const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8765');
  async function load(){await page.locator('#stock-query').fill('삼전');await page.locator('#stock-search').evaluate(f=>f.requestSubmit());await page.waitForFunction(()=>document.querySelector('#chart-legend').textContent.includes('종가'));}
  await load();assert.equal(await page.getByRole('button',{name:'추세선: 시작점과 끝점 선택',exact:true}).isVisible(),false);
  await page.getByRole('button',{name:'차트 크게 보기',exact:true}).click();
  assert(await page.getByRole('button',{name:'추세선: 시작점과 끝점 선택',exact:true}).isVisible());
  await page.waitForTimeout(300);
  assert((await page.locator('#market-chart').boundingBox()).width>1000);
  const box=await page.locator('#market-chart').boundingBox();
  await page.mouse.move(box.x+600,box.y+150);await page.mouse.down();await page.mouse.move(box.x+350,box.y+150,{steps:10});await page.mouse.up();
  await page.getByRole('button',{name:'추세선: 시작점과 끝점 선택',exact:true}).click();
  const overlay=page.locator('.drawing-overlay'),endX=(await overlay.boundingBox()).width-12;
  await overlay.click({position:{x:150,y:150}});await overlay.click({position:{x:endX,y:220}});
  assert.equal(await overlay.locator('line').count(),1);
  assert(Math.abs(Number(await overlay.locator('line').getAttribute('x2'))-endX)<2,`Endpoint ${await overlay.locator('line').getAttribute('x2')} must reach ${endX}`);
  await page.getByRole('button',{name:'수평선: 가격 위치 선택',exact:true}).click();await overlay.click({position:{x:200,y:180}});
  assert.equal(await overlay.locator('line').count(),2);
  await page.keyboard.press('Control+z');assert.equal(await overlay.locator('line').count(),1);
  await page.locator('#stock-query').focus();await page.keyboard.press('Control+z');assert.equal(await overlay.locator('line').count(),1);
  await page.getByRole('button',{name:'차트 이동',exact:true}).click();
  await page.screenshot({path:'drawing-desktop.png'});
  await page.locator('[data-range="MONTH"]').click();assert.equal(await overlay.locator('line').count(),0);
  await page.locator('[data-range="3M"]').click();assert.equal(await overlay.locator('line').count(),1);
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);
  assert(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)));
  await page.screenshot({path:'drawing-mobile.png'});
  await page.keyboard.press('Escape');assert.equal(await page.locator('.chart-expanded').count(),0);
  assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.startsWith('mini-drawings:')).length),0);
  await page.reload();await load();assert.equal(await page.locator('.drawing-overlay line').count(),0);
  await page.getByRole('button',{name:'차트 크게 보기',exact:true}).click();
  await page.locator('[data-range="ALL"]').click();
  await page.getByRole('button',{name:'수평선: 가격 위치 선택',exact:true}).click();
  await page.locator('.drawing-overlay').click({position:{x:100,y:100}});
  await page.getByRole('button',{name:'선 선택 삭제',exact:true}).click();
  await page.locator('.drawing-overlay line').last().click({force:true});
  assert.equal(await page.locator('.drawing-overlay line').count(),0);
  assert.deepEqual(errors,[]);console.log('Expand, draw, undo, timeframe isolation, no persistence, erase and mobile passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

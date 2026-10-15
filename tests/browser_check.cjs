// 로컬 서버 화면의 오류/초기화/가짜 성공 응답을 점검한다. 일부 요청은 실제 서버로 간다.
// Playwright 경로와 Edge 경로는 현재 개발 PC 기준이라 다른 PC에서는 조정해야 한다.
const { chromium } = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    await page.route('**/api/status', route=>route.fulfill({json:{configured:false,model:'test'}}));
    await page.route('**/api/chat', route=>route.fulfill({status:503,json:{error:'실제 API 키를 설정해 주세요.'}}));
    await page.goto('http://127.0.0.1:8765');
    await page.screenshot({path:'desktop-preview.png'});
    // This test exercises the placeholder setup and a simulated API success.
    await page.getByText('OpenAI · API 키 설정 필요',{exact:true}).waitFor();
    await page.getByRole('button',{name:'삼전 어때?',exact:true}).click();
    await page.locator('.message.error').filter({hasText:'실제 API 키'}).waitFor();
    await page.unroute('**/api/chat');
    await page.getByRole('button',{name:'새로운 조사'}).click();
    await page.getByText('새로운 조사를 시작할게요.',{exact:false}).waitFor();
    await page.route('**/api/chat', async route => {
      const request = route.request().postDataJSON();
      if (!request.message.includes('중기')) throw new Error('User prompt was not sent');
      await route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({
        reply:'중기 관점의 조사 계획입니다. 테스트용 모델 응답입니다.',
        company:'삼성전자', code:'005930', exchange:'KOSPI', horizon:'중장기',
        focus:['이동평균선 4개 정배열: 20일선 > 60일선 > 120일선 > 200일선'], tool_calls:[]
      })});
    });
    await page.locator('#message').fill('삼성전자 중기적으로 어때?');
    await page.locator('#send').click();
    await page.getByText('이동평균선 4개 정배열:',{exact:false}).last().waitFor();
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:844});
      if (await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw new Error('Horizontal overflow');
    }
    await page.screenshot({path:'mobile-preview.png'});
    console.log('Placeholder error, reset, simulated API answer and desktop/mobile layouts passed.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});

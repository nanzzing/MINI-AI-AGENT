const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      const errors = [], messages = [];
      let model = 'gpt-4.1-mini', provider = 'api';
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/api/**', route => {
        const pathname = new URL(route.request().url()).pathname;
        if (pathname === '/api/chat') {
          const input = route.request().postDataJSON();
          messages.push(input); model = input.api_model;
          return route.fulfill({json:{reply:'모델 선택 테스트 응답', model, focus:[], coverage:[], research:{response_mode:'concept'}, tool_calls:[], agent_steps:[]}});
        }
        return route.fulfill({json:{provider, configured:true, model,
          api_models:['gpt-4.1-mini','gpt-5.4-mini','gpt-5.4','gpt-6-luna','gpt-6-sol','gpt-6.1-sol','gpt-6-astra'].map(id => ({id,label:id})),
          research_focus:{}, stocks:[], active:[], status:'unavailable'}});
      });
      await page.goto(process.env.MINI_AGENT_URL || 'http://127.0.0.1:8767/');
      const picker = page.locator('#api-model');
      await picker.waitFor({state:'visible'});
      assert.equal(await picker.inputValue(), model);
      for (const id of ['gpt-6-luna','gpt-6-sol','gpt-6.1-sol','gpt-6-astra']) {
        await picker.selectOption(id);
        await page.locator('#message').fill('이어서 설명해줘');
        await page.locator('#message').press('Enter');
        await page.waitForFunction(() => !busy);
        assert.equal(messages.at(-1).api_model, id);
        assert.equal(await picker.inputValue(), id);
      }
      assert.equal(await page.locator('.message.assistant').count(), 4);
      await page.reload();
      await picker.waitFor({state:'visible'});
      assert.equal(await picker.inputValue(), 'gpt-6-astra');
      await picker.scrollIntoViewIfNeeded();
      // The existing market indicators stay pinned; reveal the control below them.
      if (width < 500) await page.evaluate(() => window.scrollBy(0, -220));
      await page.screenshot({path:require('node:path').join(require('node:os').tmpdir(), `mini-api-model-${width}.png`)});
      const box = await picker.boundingBox();
      assert(box.x >= 0 && box.x + box.width <= width);
      provider = 'chatgpt';
      await page.evaluate(() => refreshStatus());
      assert.equal(await page.locator('#api-model-controls').isVisible(), false);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS API model selection, reload, provider separation, desktop/mobile');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});

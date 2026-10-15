// 실행 중인 서버와 실제 Yahoo 차트를 검증한다. 채팅 응답만 대체하여 OpenAI 비용은 발생하지 않는다.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('C:/Users/Park Chan Wan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // 전체 데이터 보존과 초기 10% 표시 범위를 실제 시간축 값으로 구분해서 검사한다.
    await page.route('**/market.js', async route => {
      const response = await route.fetch();
      await route.fulfill({response, body: `
        const originalCreateChart = window.LightweightCharts.createChart;
        window.testCharts = [];
        window.LightweightCharts = {...window.LightweightCharts, createChart(...args) {
          const chart = originalCreateChart(...args);
          const addLineSeries = chart.addLineSeries.bind(chart);
          chart.testLineSeries = [];
          chart.addLineSeries = options => {
            const series = addLineSeries(options);
            chart.testLineSeries.push(series);
            return series;
          };
          window.testCharts.push(chart);
          return chart;
        }};
        ${await response.text()}`});
    });
    await page.route('**/api/discovery', route => route.fulfill({json: {news: [], active: [], fetched_at: new Date().toISOString()}}));
    await page.route('**/api/chat', route => {
      const {message} = route.request().postDataJSON();
      return route.fulfill({json: {
        reply: 'Chart integration test', company: '삼성전자', code: '005930', exchange: 'KOSPI',
        horizon: message.includes('장기') ? '장기' : '단기', research: {}, tool_calls: [], agent_steps: [], focus: [], coverage: [],
      }});
    });
    await page.goto(process.env.MINI_AGENT_URL || 'http://127.0.0.1:8766');
    const nextPrices = () => page.waitForResponse(response => response.url().includes('/api/market?'), {timeout: 90000});
    const expectedStart = bars => {
      const first = Date.parse(bars[0].time);
      const last = Date.parse(bars.at(-1).time);
      return bars.findIndex(bar => Date.parse(bar.time) >= last - (last - first) / 10);
    };
    const waitChart = async (code, range = 'RECENT') => {
      await page.waitForFunction(code => document.querySelector('#chart-title').textContent.includes(code)
        && document.querySelector('#chart-legend').textContent.length > 0
        && document.querySelector('#chart-status').textContent === '', code, {timeout: 90000});
      // 시간축 변경은 다음 렌더 프레임에 반영되므로 이전 종목의 범위를 읽지 않는다.
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.locator(`[data-range="${range}"]`).getAttribute('aria-pressed'), 'true');
      const {bars, bounds} = await page.evaluate(() => ({
        bars: window.testCharts[0].testLineSeries.find(series => series.options().color === 'transparent').data(),
        bounds: window.testCharts[0].timeScale().getVisibleLogicalRange(),
      }));
      const from = range === 'ALL' ? 0 : expectedStart(bars);
      assert(Math.abs(bounds.from - from) <= 1, `${range} must display its intended start: ${JSON.stringify(bounds)}`);
      assert(bounds.to >= bars.length - 1, 'The latest bar must remain visible');
    };

    let response = nextPrices();
    await page.locator('#stock-query').fill('AAPL');
    await page.locator('#stock-search').evaluate(form => form.requestSubmit());
    const apple = (await (await response).json()).data;
    assert.equal(apple.requested_period, 'max');
    assert(apple.bars.length > 1260, 'The chart should include more than five years for AAPL');
    await waitChart('AAPL');
    await page.locator('[data-range="ALL"]').click();
    await waitChart('AAPL', 'ALL');
    await page.locator('[data-range="RECENT"]').click();
    await waitChart('AAPL');
    const markers = await page.evaluate(() => window.testCharts[0].testLineSeries.map(series => series.options()));
    assert.equal(markers.find(options => options.color === 'transparent').crosshairMarkerVisible, false);
    assert.equal(markers.filter(options => options.visible && options.crosshairMarkerVisible).length, 4,
      'Only the four daily moving-average markers should remain');

    // 이전에 사용자가 확대했더라도 새 종목을 채팅으로 조회하면 최근 10%로 돌아온다.
    await page.locator('[data-range="3M"]').click();
    response = nextPrices();
    await page.locator('#message').fill('삼전 단기');
    await page.locator('#chat-form').evaluate(form => form.requestSubmit());
    const short = (await (await response).json()).data;
    assert.equal(short.requested_period, 'max');
    await waitChart('005930');
    await page.waitForFunction(() => !document.querySelector('#send').disabled);

    await page.locator('[data-range="1Y"]').click();
    response = nextPrices();
    await page.locator('[data-horizon="장기"]').click();
    const long = (await (await response).json()).data;
    assert.equal(long.requested_period, 'max');
    assert.equal(long.bars.length, short.bars.length);
    assert.equal(long.bars[0].time, short.bars[0].time);
    await waitChart('005930');
    await page.waitForFunction(() => !document.querySelector('#send').disabled);
    for (const range of ['3M', '1Y', 'MONTH', 'ALL', 'RECENT']) {
      await page.locator(`[data-range="${range}"]`).click();
      assert.equal(await page.locator(`[data-range="${range}"]`).getAttribute('aria-pressed'), 'true');
    }
    for (const mode of ['rsi', 'none', 'macd', 'signal', 'rsi']) {
      await page.locator('#indicator-select').selectOption(mode);
      await page.waitForTimeout(100);
      await waitChart('005930');
    }
    assert.match(await page.locator('#indicator-values').textContent(), /RSI\(14\) [\d.]+/);
    await page.getByRole('button', {name: '차트 크게 보기', exact: true}).click();
    await page.waitForTimeout(200);
    await waitChart('005930');
    await page.getByRole('button', {name: '차트 축소 (Esc)', exact: true}).click();
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      await page.locator('#market-chart').scrollIntoViewIfNeeded();
      await page.waitForTimeout(200);
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)));
      const bounds = await page.evaluate(() => window.testCharts.map(chart => chart.timeScale().getVisibleLogicalRange()));
      assert(bounds.every(range => Math.abs(range.from - expectedStart(short.bars)) <= 1),
        `Recent 10% changed at ${width}px: ${JSON.stringify(bounds)}`);
      assert(Math.abs(bounds[0].from - bounds[1].from) < 2, 'Price and indicator ranges must stay synchronized');
      assert(await page.locator('#market-chart').evaluate(root => [...root.querySelectorAll('canvas')].some(canvas => {
        const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        for (let i = 0; i < pixels.length; i += 4)
          if (pixels[i] < 80 && pixels[i + 1] > 100 && pixels[i + 1] < 170 && pixels[i + 2] < 130) return true;
        return false;
      })));
      await page.screenshot({path: path.join(os.tmpdir(), `mini-agent-recent-history-${width}.png`)});
    }
    assert.deepEqual(errors, []);
    // 종가 점을 숨겨도 가격/보조지표의 교차선 날짜는 양방향으로 연동되어야 한다.
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator('[data-range="1Y"]').click();
    for (const id of ['indicator-chart', 'market-chart']) {
      await page.locator(`#${id}`).hover({position: {x: 120, y: 65}});
      const date = (await page.locator('#chart-legend').textContent()).match(/^\d{4}-\d{2}-\d{2}/)?.[0];
      assert(date, `${id} hover should select a date`);
      assert((await page.locator('#indicator-values').textContent()).startsWith(date));
    }
    // 거래 공백이 큰 자료에서도 봉 개수의 10%가 아니라 달력 기간의 10%를 사용한다.
    const sparse = [1980, 1981, 1982, 1983, 1984, 2022, 2023, 2024, 2025, 2026].map((year, index) => ({
      time: `${year}-01-01`, open: 100 + index, high: 102 + index, low: 99 + index, close: 101 + index, volume: 1000,
    }));
    await page.route('**/api/market?**', route => route.fulfill({json: {
      stock: {code: 'AAPL', exchange: 'NASDAQ', name: 'Apple'},
      data: {bars: sparse, requested_period: 'max'}, policy: {annual_years: 0, quarters: 4},
    }}));
    await page.evaluate(() => marketView.load({code: 'AAPL', exchange: 'NASDAQ'}));
    await waitChart('AAPL');
    assert.equal(expectedStart(sparse), 5);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({apple: {from: apple.bars[0].time, recentFrom: apple.bars[expectedStart(apple.bars)].time, bars: apple.bars.length},
      samsung: {from: short.bars[0].time, recentFrom: short.bars[expectedStart(short.bars)].time, bars: short.bars.length},
      status: 'Calendar-based 10%, full history, search/chat, range controls, crosshairs and desktop/mobile passed'}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

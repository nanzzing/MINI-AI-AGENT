// 마지막 선택 종목의 실제 가격만 표시하고, 오래 걸린 이전 요청은 무시한다.
// 학습 경계: 이 파일은 차트/표 표시용 데이터 요청을 담당한다. 모델의 get_* 도구 호출은
// llm.py에서 발생한다. 차트 검색을 눌렀다고 모델에게 그 자료가 자동 전달되지는 않는다.
// 데이터 이동: load → /api/market → bars → redraw → 시리즈.setData.
// 사용자가 범위/보조지표를 바꾸면 보유 bars로 다시 그리며 매번 LLM을 호출하지 않는다.
window.marketView = (() => {
  // 즉시 실행 함수(IIFE) 내부에 차트 상태를 가둔다. 마지막 return의 메서드만 app.js에 공개한다.
  // bars는 서버에서 받은 전체 일봉, range는 화면에 보여줄 범위/월봉 모드다.
  let chart,
    candles,
    volumes,
    bars = [],
    selected = null,
    horizon = null,
    sequence = 0,
    range = "RECENT";
  const el = (id) => document.getElementById(id);
  const number = (value) =>
    Number(value).toLocaleString("ko-KR", { maximumFractionDigits: 2 });
  let indicatorChart,
    indicatorLines,
    bandLines,
    indicatorData,
    paneSelect,
    bandToggle,
    syncing = false;
  let crosshairSyncing = false,
    closeGuide,
    drawingTools;
  const dateKey = (time) =>
    typeof time === "string"
      ? time
      : time && typeof time === "object"
        ? `${time.year}-${String(time.month).padStart(2, "0")}-${String(time.day).padStart(2, "0")}`
        : null;
  function syncCrosshair(param, fromPrice) {
    // 가격 차트와 보조지표 차트는 별개 객체다. 마우스 날짜를 기준으로 상대 차트의
    // 값을 찾아 교차선을 옮긴다. fromPrice=true면 가격→보조지표 방향이다.
    // 상대 차트 이동 이벤트가 다시 이 함수를 부르는 순환을 플래그로 차단한다.
    if (crosshairSyncing) return;
    crosshairSyncing = true;
    try {
      const time = param.point ? dateKey(param.time) : null;
      const target = fromPrice ? indicatorChart : chart;
      const pricePoints = new Map(),
        indicatorPoints = new Map();
      const bar = time
        ? closeGuide.data().find((point) => point.time === time)
        : null;
      const candle = time
        ? candles.data().find((point) => point.time === time)
        : null;
      if (candle) pricePoints.set(candles, candle);
      for (const item of averages) {
        const point = item.points.find((p) => p.time === time);
        if (point) pricePoints.set(item.series, point);
      }
      for (const [key, series] of Object.entries(indicatorLines)) {
        const point = indicatorData?.[key].find((p) => p.time === time);
        if (point) indicatorPoints.set(series, point);
      }
      const mode = paneSelect.value,
        key = mode === "rsi" ? "rsi" : mode === "signal" ? "signal" : "macd";
      const point = fromPrice ? indicatorPoints.get(indicatorLines[key]) : bar;
      if (time && point && mode !== "none")
        target.setCrosshairPosition(
          point.value,
          time,
          fromPrice ? indicatorLines[key] : closeGuide,
        );
      else target.clearCrosshairPosition();
      const priceParam = { time: time || undefined, seriesData: pricePoints };
      averageLegend(priceParam);
      if (candle)
        el("chart-legend").textContent =
          `${time}  시 ${number(candle.open)} · 고 ${number(candle.high)} · 저 ${number(candle.low)} · 종 ${number(candle.close)}`;
      else
        el("chart-legend").textContent = bars.length
          ? `종가 ${number(bars.at(-1).close)} · 거래량 ${number(bars.at(-1).volume)}`
          : "";
      indicatorLegend({ time: time || undefined, seriesData: indicatorPoints });
    } finally {
      crosshairSyncing = false;
    }
  }
  function updateIndicators(visible) {
    // visible은 '화면에 잘린 구간'이 아니라 현재 봉 주기의 전체 자료다.
    // 지표 준비 기간도 날짜 슬롯을 남겨 두 차트의 logical index가 어긋나지 않게 한다.
    const bounds = chart.timeScale().getVisibleLogicalRange();
    const previousSyncing = syncing;
    syncing = true;
    try {
      indicatorData = calculateIndicators(visible);
      for (const key of ["upper", "middle", "lower"]) {
        bandLines[key].setData(indicatorData[key]);
        bandLines[key].applyOptions({ visible: bandToggle.checked });
      }
      const mode = paneSelect.value;
      const panel = el("indicator-panel");
      const opening = panel.hidden && mode !== "none";
      panel.hidden = mode === "none";
      if (opening) {
        // 숨겨진 차트의 너비는 0이다. 실제 크기를 먼저 맞춰야 전체 기간이
        // 최소 봉 간격으로 계산되어 가격 차트까지 과도하게 축소되는 일을 막는다.
        const surface = el("indicator-chart");
        indicatorChart.applyOptions({ autoSize: false });
        indicatorChart.resize(surface.clientWidth, surface.clientHeight);
        indicatorChart.applyOptions({ autoSize: true });
      }
      for (const [key, series] of Object.entries(indicatorLines)) {
        // 준비 기간의 빈 점도 포함해 가격 차트와 날짜 인덱스를 일치시킨다.
        const points = new Map(indicatorData[key].map((p) => [p.time, p]));
        series.setData(
          visible.map((bar) => points.get(bar.time) || { time: bar.time }),
        );
        series.applyOptions({
          visible:
            mode === "rsi"
              ? key === "rsi"
              : mode === "macd"
                ? key !== "rsi"
                : mode === key,
        });
      }
      if (bounds && !panel.hidden) indicatorChart.timeScale().setVisibleLogicalRange(bounds);
    } finally {
      syncing = previousSyncing;
    }
    indicatorLegend();
  }
  function indicatorLegend(param) {
    // 마우스가 특정 날짜에 있으면 그 날짜 값, 아니면 마지막 지표 값을 표시한다.
    // 값이 없는 초기 기간은 0이 아니라 대시로 표시한다.
    const keys =
      paneSelect.value === "rsi"
        ? ["rsi"]
        : paneSelect.value === "signal"
          ? ["signal"]
          : ["macd", "signal", "histogram"];
    const names = {
      rsi: "RSI(14)",
      macd: "MACD(12,26)",
      signal: "Signal(9)",
      histogram: "Histogram",
    };
    el("indicator-values").textContent =
      (param?.time ? `${dateKey(param.time)} · ` : "") +
      keys
        .map((key) => {
          const point = param?.time
            ? param.seriesData.get(indicatorLines[key])
            : indicatorData?.[key].at(-1);
          return `${names[key]} ${point ? number(point.value) : "—"}`;
        })
        .join(" · ");
  }
  const averages = [
    { period: 20, color: "#eab76d" },
    { period: 60, color: "#69b5eb" },
    { period: 120, color: "#bb96df" },
    { period: 200, color: "#aab4ae" },
    { period: 5, color: "#eab76d", monthly: true },
  ];
  function averageLegend(param) {
    // 이평선 숫자 범례도 같은 날짜 이벤트를 사용해 차트 선과 표시 숫자를 맞춘다.
    for (const item of averages) {
      const point = param?.time
        ? param.seriesData.get(item.series)
        : item.points.at(-1);
      item.value.textContent = point ? number(point.value) : "—";
    }
  }
  function init() {
    // 처음 그릴 때 한 번만 차트/시리즈/컨트롤을 만든다. 이후 redraw는 setData로 갱신한다.
    // LightweightCharts는 렌더링 라이브러리이며 주가를 수집하거나 모델을 호출하지 않는다.
    chart = LightweightCharts.createChart(el("market-chart"), {
      autoSize: true,
      layout: {
        background: { color: "#111513" },
        textColor: "#a3b5a9",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#29332c" },
        horzLines: { color: "#29332c" },
      },
      rightPriceScale: { borderVisible: false },
      // 기본 최소 봉 간격(0.5px)은 수십 년 이력을 한 화면에 담지 못한다.
      timeScale: { borderVisible: false, minBarSpacing: 0.001, lockVisibleTimeRangeOnResize: true },
      localization: { locale: "ko-KR" },
    });
    candles = chart.addCandlestickSeries({
      upColor: "#71edbd",
      downColor: "#e88d98",
      borderVisible: false,
      wickUpColor: "#71edbd",
      wickDownColor: "#e88d98",
    });
    candles
      .priceScale()
      .applyOptions({ scaleMargins: { top: 0.08, bottom: 0.27 } });
    // 종가 자료는 보조지표와 교차선을 연동할 때만 사용하고 선과 점은 숨긴다.
    closeGuide = chart.addLineSeries({
      color: "transparent",
      lineVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    volumes = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    volumes
      .priceScale()
      .applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const legend = document.createElement("div");
    legend.id = "ma-legend";
    el("market-chart").before(legend);
    for (const item of averages) {
      item.series = chart.addLineSeries({
        color: item.color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
      });
      item.points = [];
      const label = document.createElement("label"),
        toggle = document.createElement("input"),
        swatch = document.createElement("span");
      toggle.type = "checkbox";
      toggle.checked = true;
      toggle.setAttribute(
        "aria-label",
        `${item.period}${item.monthly ? "개월" : "일"} 이동평균선`,
      );
      swatch.className = "ma-swatch";
      swatch.style.backgroundColor = item.color;
      item.value = document.createElement("output");
      item.label = label;
      item.toggle = toggle;
      label.append(
        toggle,
        swatch,
        `${item.period}${item.monthly ? "개월" : "일"}`,
        item.value,
      );
      legend.append(label);
      toggle.addEventListener("change", () =>
        item.series.applyOptions({ visible: toggle.checked }),
      );
    }
    const controls = document.createElement("div");
    controls.className = "indicator-controls";
    const selectLabel = document.createElement("label");
    selectLabel.textContent = "보조지표 ";
    paneSelect = document.createElement("select");
    paneSelect.id = "indicator-select";
    paneSelect.setAttribute("aria-label", "보조지표");
    for (const [value, text] of [
      ["none", "없음"],
      ["rsi", "RSI (14)"],
      ["macd", "MACD + Signal"],
      ["signal", "MACD Signal (9)"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      paneSelect.append(option);
    }
    selectLabel.append(paneSelect);
    const bandLabel = document.createElement("label");
    bandToggle = document.createElement("input");
    bandToggle.type = "checkbox";
    bandToggle.id = "bollinger-toggle";
    bandLabel.append(bandToggle, "볼린저 (20, 2)");
    controls.append(selectLabel, bandLabel);
    legend.before(controls);
    bandLines = {};
    for (const [key, color] of Object.entries({
      upper: "#b25c85",
      middle: "#837044",
      lower: "#b25c85",
    }))
      bandLines[key] = chart.addLineSeries({
        color,
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        visible: false,
      });
    const panel = document.createElement("section");
    panel.id = "indicator-panel";
    panel.hidden = true;
    const values = document.createElement("div");
    values.id = "indicator-values";
    const surface = document.createElement("div");
    surface.id = "indicator-chart";
    panel.append(values, surface);
    el("market-chart").after(panel);
    indicatorChart = LightweightCharts.createChart(surface, {
      autoSize: true,
      layout: {
        background: { color: "#111513" },
        textColor: "#a3b5a9",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#29332c" },
        horzLines: { color: "#29332c" },
      },
      rightPriceScale: { minimumWidth: 70 },
      timeScale: { borderVisible: false, minBarSpacing: 0.001, lockVisibleTimeRangeOnResize: true },
    });
    chart.priceScale("right").applyOptions({ minimumWidth: 70 });
    indicatorChart
      .timeScale()
      .applyOptions({ lockVisibleTimeRangeOnResize: true });
    indicatorLines = {};
    for (const [key, color] of Object.entries({
      rsi: "#bb96df",
      macd: "#69b5eb",
      signal: "#eab76d",
    }))
      indicatorLines[key] = indicatorChart.addLineSeries({
        color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        ...(key === "rsi"
          ? {
              autoscaleInfoProvider: () => ({
                priceRange: { minValue: 0, maxValue: 100 },
              }),
            }
          : {}),
      });
    indicatorLines.histogram = indicatorChart.addHistogramSeries({
      priceLineVisible: false,
      lastValueVisible: false,
    });
    for (const price of [30, 70])
      indicatorLines.rsi.createPriceLine({
        price,
        color: "#9b4ab577",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: false,
      });
    for (const [source, target] of [
      [chart, indicatorChart],
      [indicatorChart, chart],
    ])
      source.timeScale().subscribeVisibleLogicalRangeChange((bounds) => {
        if (!bounds || syncing || panel.hidden) return;
        // 준비 기간이 부족해 값이 없는 보조지표는 가격 차트의 범위를 바꾸지 않는다.
        if (source === indicatorChart && !Object.entries(indicatorLines).some(
          ([key, series]) => series.options().visible && indicatorData?.[key]?.length,
        )) return;
        syncing = true;
        try {
          target.timeScale().setVisibleLogicalRange(bounds);
        } finally {
          syncing = false;
        }
      });
    indicatorChart.subscribeCrosshairMove((param) =>
      syncCrosshair(param, false),
    );
    paneSelect.addEventListener("change", () =>
      updateIndicators(range === "MONTH" ? monthly() : bars),
    );
    bandToggle.addEventListener("change", () => {
      for (const series of Object.values(bandLines))
        series.applyOptions({ visible: bandToggle.checked });
    });
    chart.subscribeCrosshairMove((param) => syncCrosshair(param, true));
    drawingTools = createDrawingTools(chart, candles, el("market-chart"));
  }
  function monthly() {
    // 일봉을 월별 OHLCV로 합친다: 첫 시가, 최대 고가, 최소 저가, 마지막 종가, 거래량 합계.
    // 화면은 현재 월의 진행 중 봉도 포함한다. Python의 월봉 안착 판정은 현재 월을 제외한다.
    const result = [];
    for (const bar of bars) {
      const key = bar.time.slice(0, 7),
        previous = result.at(-1);
      if (previous?.key === key) {
        previous.high = Math.max(previous.high, bar.high);
        previous.low = Math.min(previous.low, bar.low);
        previous.close = bar.close;
        previous.volume += bar.volume;
      } else result.push({ ...bar, key, time: `${key}-01` });
    }
    return result;
  }
  function redraw() {
    // 봉 주기 전환/새 가격 수신 시 호출한다. 자료 계산과 보이는 범위 설정을 구분한다.
    // 전체 이력으로 지표를 계산한 다음 3개월/1년 등의 보이는 범위만 좁힌다.
    if (!chart) init();
    const visible = range === "MONTH" ? monthly() : bars;
    candles.setData(visible);
    closeGuide.setData(
      visible.map((bar) => ({ time: bar.time, value: bar.close })),
    );
    // 화면에 보이는 구간이 아닌 전체 이력으로 SMA를 계산해 왼쪽 경계도 정확히 표시한다.
    for (const item of averages) {
      const active = Boolean(item.monthly) === (range === "MONTH");
      item.label.hidden = !active;
      item.points = [];
      if (active) {
        let sum = 0;
        visible.forEach((bar, index) => {
          sum += bar.close;
          if (index >= item.period) sum -= visible[index - item.period].close;
          if (index >= item.period - 1)
            item.points.push({ time: bar.time, value: sum / item.period });
        });
      }
      item.series.setData(item.points);
      item.series.applyOptions({ visible: active && item.toggle.checked });
    }
    averageLegend();
    volumes.setData(
      visible.map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? "#21835e44" : "#d6757044",
      })),
    );
    updateIndicators(visible);
    if (visible.length) {
      const count = range === "3M" ? 65 : range === "1Y" ? 252 : visible.length;
      let from = Math.max(-1, visible.length - count);
      if (range === "RECENT") {
        // 봉 개수 대신 실제 날짜 간격의 최근 10%를 보여준다. 전체 원본은 유지한다.
        const first = Date.parse(visible[0].time);
        const last = Date.parse(visible.at(-1).time);
        const cutoff = last - (last - first) / 10;
        from = visible.findIndex((bar) => Date.parse(bar.time) >= cutoff);
      }
      const bounds = { from, to: visible.length + 2 };
      const previousSyncing = syncing;
      syncing = true;
      try {
        chart.timeScale().setVisibleLogicalRange(bounds);
        // 종목 변경 때 보조지표의 이전 범위가 새 가격 차트에 되돌아오지 않게 맞춘다.
        if (!el("indicator-panel").hidden) indicatorChart.timeScale().setVisibleLogicalRange(bounds);
      } finally {
        syncing = previousSyncing;
      }
    }
    document
      .querySelectorAll("[data-range]")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.range === range),
        ),
      );
    if (bars.length)
      el("chart-legend").textContent =
        range === "MONTH"
          ? "월봉 · 현재 월은 미완성 캔들입니다."
          : `종가 ${number(bars.at(-1).close)} · 거래량 ${number(bars.at(-1).volume)}`;
    drawingTools.context(
      selected
        ? `mini-drawings:${selected.exchange}:${selected.code}:${range === "MONTH" ? "monthly" : "daily"}`
        : "",
    );
    drawingTools.render();
  }
  function clearData() {
    // 종목이 바뀔 때 이전 종목의 재무/뉴스가 새 차트 밑에 남지 않도록 지운다.
    el("financial-result").replaceChildren();
    el("news-result").textContent = "투자 관점 확정 후 조회합니다.";
    el("news-period").textContent = "";
    el("events-section").hidden = true;
  }
  async function load(params) {
    // horizon은 재무 범위 표시에만 쓰고, 차트 GET은 항상 전체 가격 이력을 반환한다.
    // 새 검색/채팅 요청은 전체 기간의 최근 10%로 시작하고, 원본은 전체 보기/이동에 사용한다.
    const request = ++sequence;
    // 요청 번호를 캡처한다. 나중에 보낸 요청이 먼저 끝나면 옛 응답이 화면을 덮지 못하게 한다.
    el("chart-status").textContent = "가격 데이터를 불러오는 중…";
    range = "RECENT";
    bars = [];
    redraw();
    try {
      const response = await fetch(
          `/api/market?${new URLSearchParams(params)}`,
        ),
        result = await response.json();
      if (request !== sequence) return;
      if (!response.ok) throw new Error(result.error);
      if (!result.stock) {
        clear();
        el("chart-status").textContent =
          result.status === "ambiguous"
            ? "종목코드로 검색해 주세요."
            : "일치하는 종목이 없어요.";
        return;
      }
      if (
        selected?.code !== result.stock.code ||
        selected?.exchange !== result.stock.exchange
      )
        clearData();
      selected = result.stock;
      horizon = params.horizon || null;
      el("context-company").textContent = selected.name;
      el("context-code").textContent =
        `${selected.code} · ${selected.exchange}`;
      el("chart-title").textContent = `${selected.name} · ${selected.code}`;
      el("chart-currency").textContent = result.data.currency || "";
      el("price-asof").textContent = result.data.as_of
        ? `${result.data.as_of} 기준`
        : "";
      el("price-source").href = result.data.url || "https://finance.yahoo.com/";
      bars = result.data.bars || [];
      el("chart-status").textContent = bars.length
        ? ""
        : result.data.message || "가격 데이터 없음";
      el("chart-legend").textContent = bars.length
        ? `종가 ${number(bars.at(-1).close)} · 거래량 ${number(bars.at(-1).volume)}`
        : "";
      const rules = result.policy;
      el("financial-policy").textContent = horizon
        ? `요청 범위: ${rules.annual_years ? `최근 ${rules.annual_years}개년 + ` : ""}${rules.quarters}분기 · 필요 시 조회`
        : "투자 관점을 선택해 주세요.";
      redraw();
    } catch (error) {
      if (request === sequence) {
        clear();
        el("chart-status").textContent = error.message || "차트 조회 실패";
      }
    }
  }
  function clear() {
    // sequence도 올려 진행 중인 옛 요청을 무효화한다. 서버의 대화 세션을 지우는 함수는 아니다.
    sequence++;
    bars = [];
    selected = null;
    horizon = null;
    redraw();
    el("chart-status").textContent = "종목을 선택해 주세요.";
    el("chart-title").textContent = "PRICE / VOLUME";
    el("chart-legend").textContent = "";
    el("price-asof").textContent = "";
    el("chart-currency").textContent = "";
    clearData();
    el("financial-policy").textContent = "투자 관점을 선택해 주세요.";
  }
  function showFinancials(data) {
    // 기간을 열, 재무 항목을 행으로 표시한다. 누락값은 대시이며 0으로 만들지 않는다.
    // 표를 HTML 문자열로 조립하지 않고 DOM/textContent로 만들어 외부 자료를 안전하게 표시한다.
    const target = el("financial-result");
    target.replaceChildren();
    const note = document.createElement("p");
    note.className = "data-warning";
    note.textContent = `${data.source} · ${data.currency || "통화 미확인"} · 금액: 통화 기본 단위 (EPS·주식수 별도) · ${(data.missing || []).join(" / ") || "요청 범위 수집 완료"}`;
    target.append(note);
    const labels = {
      income: "손익계산서",
      balance: "재무상태표",
      cashflow: "현금흐름표",
    };
    for (const [frequency, section] of Object.entries(data.statements || {}))
      for (const [kind, rows] of Object.entries(section)) {
        const details = document.createElement("details"),
          summary = document.createElement("summary");
        summary.textContent = `${frequency === "yearly" ? "연간" : "분기"} ${labels[kind]} · ${rows.length}기간`;
        details.append(summary);
        const wrap = document.createElement("div");
        wrap.className = "financial-table-wrap";
        const table = document.createElement("table"),
          header = table.insertRow();
        ["항목", ...rows.map((row) => row.period_end)].forEach((text) => {
          const th = document.createElement("th");
          th.textContent = text;
          header.append(th);
        });
        const fields = [
          ...new Set(rows.flatMap((row) => Object.keys(row.values))),
        ];
        for (const field of fields) {
          const tr = table.insertRow();
          [
            field,
            ...rows.map((row) =>
              row.values[field] === undefined ? "—" : number(row.values[field]),
            ),
          ].forEach((text) => {
            tr.insertCell().textContent = text;
          });
        }
        wrap.append(table);
        details.append(wrap);
        target.append(details);
      }
  }
  function research(data) {
    // /api/chat의 research 자료로 재무·뉴스·일정 영역을 갱신한다.
    // 링크는 HTTPS만 연결하고 새 창에는 opener 접근을 허용하지 않는다.
    if (!data) return;
    if (data.financials) showFinancials(data.financials);
    if (data.news) {
      const target = el("news-result");
      target.replaceChildren();
      el("news-period").textContent = `최근 ${data.news.days || ""}일`;
      for (const item of data.news.items || []) {
        const article = document.createElement("article"),
          link = document.createElement("a");
        link.textContent = item.title;
        if (item.url.startsWith("https://")) link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const small = document.createElement("small");
        small.textContent = `${item.publisher} · ${item.published_at.slice(0, 10)}`;
        article.append(link, small);
        target.append(article);
      }
      if (!target.children.length)
        target.textContent =
          data.news.status === "empty"
            ? "기간 내 검색 결과가 없어요."
            : data.news.message || "뉴스 조회 실패";
    }
    if (data.events) {
      el("events-section").hidden = data.events.status === "not_requested";
      el("events-result").textContent = data.events.items?.length
        ? data.events.items
            .map((item) => `${item.date} · ${item.event}`)
            .join("\n")
        : data.events.message || data.events.note;
    }
  }
  document.querySelectorAll("[data-range]").forEach((button) =>
    button.addEventListener("click", () => {
      range = button.dataset.range;
      redraw();
      if (range === "MONTH")
        el("chart-legend").textContent = "월봉 · 현재 월은 미완성 캔들입니다.";
    }),
  );
  el("stock-search").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = el("stock-query").value.trim();
    if (query) load({ query, ...(horizon ? { horizon } : {}) });
  });
  el("load-financials").addEventListener("click", async () => {
    // 재무표를 수동 조회하는 화면 버튼이다. 모델 get_financials 요청과는 별개 경로다.
    if (!selected || !horizon) {
      el("financial-result").textContent =
        "먼저 채팅에서 종목과 투자 관점을 정해 주세요.";
      return;
    }
    const identity = selected.code + selected.exchange + horizon;
    // 요청 도중 종목이나 관점이 바뀌면 이전 종목의 재무 응답을 현재 화면에 표시하지 않는다.
    el("financial-result").textContent = "재무제표를 불러오는 중…";
    el("load-financials").disabled = true;
    try {
      const response = await fetch(
          `/api/financials?${new URLSearchParams({ code: selected.code, exchange: selected.exchange, horizon })}`,
        ),
        result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (identity === selected?.code + selected?.exchange + horizon)
        showFinancials(result.data);
    } catch (error) {
      el("financial-result").textContent = error.message;
    } finally {
      el("load-financials").disabled = false;
    }
  });
  return {
    // app.js에 공개하는 좁은 인터페이스. context는 채팅에서 확정된 종목이 차트 선택과
    // 다를 때 가격을 다시 읽고, 같은 문맥의 조사 자료만 화면에 붙인다.
    load,
    clear,
    research,
    async context(data) {
      if (!data.code) return;
      const changed =
        selected?.code !== data.code ||
        selected?.exchange !== data.exchange ||
        horizon !== data.horizon;
      if (changed) {
        clearData();
        await load({
          code: data.code,
          exchange: data.exchange,
          ...(data.horizon ? { horizon: data.horizon } : {}),
        });
      }
      if (
        selected?.code === data.code &&
        selected?.exchange === data.exchange &&
        horizon === data.horizon
      )
        research(data.research);
    },
  };
})();

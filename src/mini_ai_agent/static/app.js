/**
 * 대화 화면의 진입점. HTML이 준비된 뒤(defer) 실행된다.
 * 사용자 입력 → POST /api/chat → 답변/도구 내역/체크리스트 갱신을 담당한다.
 * API 키나 모델 SDK는 브라우저에 없다. 모델과 외부 데이터 호출은 Python 서버가 한다.
 * 차트는 marketView에 위임하며, 앱의 전역 상태와 서버의 세션 상태는 별개다.
 */
const $ = (selector) => document.querySelector(selector);
let busy = false;
let selectedHorizon = null;
let researchFocus = {};
let currentCoverage = [];
// busy는 중복 전송 방지, selectedHorizon은 화면의 선택값이다.
// researchFocus는 /api/status로 받은 Python 기준표, currentCoverage는 최신 답변의 근거 선언이다.
// 새로고침하면 이 변수들은 초기화되지만 서버 쿠키/대화까지 초기화되지는 않는다.
const icons = () => window.lucide?.createIcons();

async function refreshStatus() {
  // 키 원문 없이 설정 여부와 기준표만 받는다. configured는 인증 검증 결과가 아니다.
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error("status");
    const data = await response.json();
    researchFocus = data.research_focus || researchFocus;
    $(".connection").textContent = data.configured
      ? `OpenAI · ${data.model}`
      : "OpenAI · API 키 설정 필요";
    $(".data-note").textContent =
      "Yahoo Finance 가격·재무 / Google News · 제공 시점 기준";
  } catch {
    $(".connection").textContent = "서버 연결 확인 필요";
  }
}

function addMessage(text, type = "assistant") {
  // 모델/사용자 문자열은 HTML로 해석하지 않는다. text node를 사용해 스크립트 삽입을 막는다.
  // 생성한 요소를 반환하므로 호출자가 그 답변 아래에 도구 내역을 붙일 수 있다.
  $("#welcome")?.remove();
  const node = document.createElement("div");
  node.className = `message ${type}`;
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = type === "user" ? "YOU" : "MINI / RESEARCH ASSISTANT";
  node.append(label, document.createTextNode(text));
  $("#messages").append(node);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return node;
}

function renderAgentSteps(message, steps = []) {
  // 서버가 관찰한 이벤트 배열을 순서대로 그린다. SSE/WebSocket 스트리밍이 아니므로
  // POST 응답이 완료된 뒤 한 번에 나타난다. 모델의 비공개 사고를 보여주는 기능은 아니다.
  // round는 모델 API 호출 번호다. 한 round 안에 여러 도구 실행이 들어갈 수 있다.
  if (!steps.length) return;
  const panel = document.createElement("details");
  panel.className = "tool-trace";
  panel.open = true;
  const summary = document.createElement("summary");
  summary.textContent = `Agent Loop · 모델 호출 ${steps.filter((step) => step.event === "모델 호출").length}회`;
  panel.append(summary);
  const note = document.createElement("p");
  note.textContent = "답변 완료 후 표시하는 실제 실행 기록입니다. 모델의 내부 사고 과정은 아닙니다.";
  panel.append(note);
  const list = document.createElement("ol");
  for (const step of steps) {
    // 서버 문자열도 신뢰한 HTML로 넣지 않는다. textContent로 표시하면 태그는 글자가 된다.
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = `${step.round}차 · ${step.event}`;
    const detail = document.createElement("p");
    detail.textContent = step.detail;
    item.append(title, detail);
    if (step.available_tools?.length) {
      const available = document.createElement("p");
      available.textContent = `재사용 가능한 보유 자료: ${step.available_tools.join(", ")}`;
      item.append(available);
    }
    list.append(item);
  }
  panel.append(list);
  message.append(panel);
}

function renderToolCalls(message, calls = [], research = {}) {
  // 같은 round의 요청은 한 모델 응답에서 왔다. 결과는 다음 모델 호출에 전달된다.
  const panel = document.createElement("details");
  panel.className = "tool-trace";
  const summary = document.createElement("summary");
  summary.textContent = `모델 도구 요청 · ${calls.length}회`;
  panel.append(summary);
  if (research.reused_tools?.length) {
    // 재제공한 사실과 모델이 사용 근거로 명시한 사실을 구분한다. 후자는 coverage가 담당한다.
    const reuse = document.createElement("p");
    reuse.textContent = `기존 자료 재제공 · ${research.reused_tools.join(", ")} · 신규 호출 없이 사용 가능했던 자료입니다. 실제 근거 명시는 체크리스트에서 확인하세요.`;
    panel.append(reuse);
  }
  if (research.financials) {
    const financials = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent = `모델 요청으로 조회한 재무 · ${research.financials_context_key?.[2] || ""} · ${research.financials.status}`;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(research.financials, null, 2);
    financials.append(title, pre);
    panel.append(financials);
  }
  if (!calls.length) {
    const empty = document.createElement("p");
    empty.textContent = "이 답변에는 모델의 도구 요청이 없습니다.";
    panel.append(empty);
  }
  calls.forEach((call, index) => {
    const entry = document.createElement("section");
    const heading = document.createElement("h3");
    const status = call.result?.error
      ? "입력·실행 오류"
      : call.result?.status || "결과 반환";
    heading.textContent = `${index + 1}. ${call.name} · 왕복 ${call.round ?? "-"} · ${status} · ${call.duration_ms ?? 0} ms`;
    entry.append(heading);
    for (const [label, value] of [
      ["모델 → 파이썬: 도구 요청 인자", call.arguments],
      ["파이썬 → 모델: 실행 결과 (다음 호출에 전달)", call.result],
    ]) {
      const details = document.createElement("details");
      const title = document.createElement("summary");
      title.textContent = label;
      const pre = document.createElement("pre");
      pre.textContent = JSON.stringify(value ?? {}, null, 2);
      details.append(title, pre);
      entry.append(details);
    }
    panel.append(entry);
  });
  message.append(panel);
}

function renderCriteria(horizon, focus, coverage = []) {
  // 관점 변경 직후에는 coverage 없이 대기 표시, 답변 완료 후에는 서버의 선언 결과를 받는다.
  // 중기는 화면 명칭이고 Python은 중장기로 보관한다. 이 경계에서 표시 이름을 통일한다.
  currentCoverage = coverage;
  selectedHorizon = horizon === "중장기" ? "중기" : horizon;
  const items =
    focus ??
    researchFocus[selectedHorizon === "중기" ? "중장기" : selectedHorizon] ??
    [];
  $("#sidebar-horizon").textContent = selectedHorizon
    ? `${selectedHorizon} 관점`
    : "투자 관점 미정";
  $("#context-horizon").textContent = selectedHorizon || "관점 미정";
  document
    .querySelectorAll("[data-horizon]")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.horizon === selectedHorizon),
      ),
    );
  $("#criteria").replaceChildren();
  const labels = {
    // declared/not_declared는 답변 생성 모델의 선언 유무다. 의미적 타당성을 검증한 등급이 아니다.
    // 아래 과거 상태명은 이전 형식의 응답을 표시할 때를 위한 호환 문구다.
    declared: "O 모델이 사용 근거로 명시",
    not_declared: "X 제공됨 · 사용 근거 미명시",
    supported: "O 제공됨 · 사용 근거 확인",
    partial: "△ 제공됨 · 일부 사용 근거 확인",
    unused: "X 제공됨 · 답변에서 사용 근거 없음",
    mismatch: "! 제공됨 · 답변과 자료 불일치",
    no_data: "근거 자료 미제공",
    unknown: "제공됨 · 사용 여부 미확인",
  };
  const explanations = {
    declared:
      "답변을 생성한 모델이 아래 분석·결론에 이 자료를 사용했다고 함께 반환했습니다. ID와 문장 존재를 검증했으며 내부 사고나 사실성을 검증한 것은 아닙니다.",
    not_declared:
      "자료는 모델에 제공됐으나 모델이 답변의 사용 근거 목록에 포함하지 않았습니다. 내부 추론에서 사용하지 않았다고 단정할 수는 없습니다.",
    supported: "모델에 제공한 자료와 연결되는 답변 근거를 확인했습니다.",
    partial: "모델에 제공한 자료 중 일부만 답변 근거로 연결됩니다.",
    unused:
      "자료는 모델에 제공됐으나, 이번 답변에서 그 자료를 사용한 근거를 찾지 못했습니다. 내부 추론에서 사용하지 않았다고 단정하는 것은 아닙니다.",
    mismatch:
      "자료는 모델에 제공됐으나, 답변의 수치 또는 해석이 해당 자료와 일치하지 않습니다.",
    no_data:
      "이번 조사에서 이 항목을 뒷받침할 유효한 자료가 제공되지 않았습니다.",
    unknown:
      "자료는 제공됐으나 모델이 반환한 자료 ID 또는 답변 문장 연결을 검증하지 못했습니다. 미사용 판정이 아닙니다.",
  };
  for (const item of items) {
    const row = document.createElement("li");
    row.className = "criterion-row";
    const check = coverage.find((entry) => entry.criterion === item);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const label = document.createElement("span");
    label.className = "criterion-label";
    label.textContent = item;
    const badge = document.createElement("span");
    badge.className = `criterion-status ${check?.status || "pending"}`;
    badge.textContent = check
      ? labels[check.status] || "사용 여부 미확인"
      : "답변 대기";
    badge.title =
      explanations[check?.status] ||
      "답변과 함께 모델이 반환하는 근거 연결을 기다립니다.";
    summary.append(label, badge);
    details.append(summary);
    if (check) {
      const explanation = document.createElement("p");
      explanation.textContent =
        explanations[check.status] || "사용 여부를 확인할 수 없습니다.";
      details.append(explanation);
      const reason = document.createElement("p");
      reason.textContent = check.reason;
      details.append(reason);
      for (const claim of check.claims || []) {
        // 한 문단이 여러 지표를 종합했더라도 해당 문단은 그대로 보여준다.
        // 전체 결론 자료는 반복하지 않고 아래에서 현재 체크리스트 항목 자료만 표시한다.
        const conclusion = document.createElement("blockquote");
        conclusion.textContent = claim.text;
        details.append(conclusion);
      }
      if (check.evidence) {
        const evidence = document.createElement("blockquote");
        evidence.textContent = check.evidence;
        details.append(evidence);
      }
      const sources = check.sources?.length
        // 사용 명시 자료가 없으면, 모델에게 어떤 자료를 제공했는지는 확인할 수 있게 한다.
        ? check.sources
        : check.available_sources || [];
      if (sources.length) {
        const sourceDetails = document.createElement("details");
        const sourceTitle = document.createElement("summary");
        sourceTitle.textContent = check.sources?.length
          ? "현재 항목의 근거 자료"
          : "현재 항목에 제공된 자료";
        sourceDetails.append(sourceTitle);
        for (const source of sources) {
          const pre = document.createElement("pre");
          pre.className = "criterion-source";
          pre.textContent = JSON.stringify(source, null, 2);
          sourceDetails.append(pre);
        }
        details.append(sourceDetails);
      }
    }
    row.append(details);
    $("#criteria").append(row);
  }
  if (!items.length) {
    const row = document.createElement("li");
    row.className = "empty-criteria";
    row.textContent = "투자 관점을 선택하면 조사 항목이 표시됩니다.";
    $("#criteria").append(row);
  }
  const supported = coverage.filter(
    (entry) => entry.status === "declared" && items.includes(entry.criterion),
  ).length;
  $("#criteria-count").textContent = items.length
    ? `${selectedHorizon} · ${coverage.length ? `${supported}/${items.length} 사용 명시` : `${items.length}개 항목`}`
    : "—";
}

function renderContext(data) {
  // 서버에서 확정한 종목·관점을 화면에 반영한다. 시장 화면에 넘기는 research는
  // 이미 조회한 결과라서 뉴스/재무를 표시하기 위해 다시 모델을 호출하지 않는다.
  $("#sidebar-company").textContent = data.company || "선택한 종목 없음";
  $("#context-company").textContent = data.company || "조사 컨텍스트";
  $("#context-code").textContent = data.code
    ? `${data.code} · ${data.exchange}`
    : "종목 미선택";
  renderCriteria(
    data.horizon || selectedHorizon,
    data.horizon ? data.focus : undefined,
    data.coverage || [],
  );
  marketView.context(data);
}

async function send(message, reset = false) {
  // 한 요청 동안 입력 버튼을 잠그고 finally에서 반드시 복구한다.
  // 이 함수의 fetch는 같은 출처이므로 브라우저가 세션 쿠키를 자동으로 전송한다.
  if (busy || !message.trim()) return;
  busy = true;
  document
    .querySelectorAll("button")
    .forEach((button) => (button.disabled = true));
  $("#composer-status").textContent = "응답을 준비하고 있어요…";
  const previousHorizon = selectedHorizon;
  // 즉시 갱신한 체크리스트는 낙관적 화면 변경이다. 요청 실패 시 이전 상태로 되돌린다.
  const previousFocus = Array.from(
    document.querySelectorAll("#criteria .criterion-label"),
    (item) => item.textContent,
  );
  const previousCoverage = currentCoverage;
  const horizons = [
    // 중장기에 포함된 '장기'를 별개 선택으로 세지 않도록 먼저 중기로 치환한다.
    ...new Set(
      message.replaceAll("중장기", "중기").match(/단기|중기|장기/g) || [],
    ),
  ];
  if (reset) renderCriteria(null, []);
  else if (horizons.length === 1) {
    await statusReady;
    renderCriteria(horizons[0]);
  } else renderCriteria(selectedHorizon, previousFocus);
  if (!reset) addMessage(message, "user");
  if (reset) marketView.clear();
  // 명확히 입력된 종목의 차트는 모델 응답을 기다리지 않고 조회한다.
  if (!reset && !/^(단기|중기|중장기|장기)$/.test(message.trim()))
    marketView.load({
      query: message,
      ...(selectedHorizon ? { horizon: selectedHorizon } : {}),
    });
  try {
    const response = await fetch("/api/chat", {
      // 서버가 요구하는 JSON 입력은 message 하나다. 이전 대화 전체를 브라우저가 보내지
      // 않아도 Python 세션이 history를 가지고 있어서 후속 질문을 이어갈 수 있다.
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.error || "요청을 처리하지 못했어요.");
    if (reset) $("#messages").replaceChildren();
    const reply = addMessage(data.reply);
    if (!reset) renderAgentSteps(reply, data.agent_steps);
    if (!reset) renderToolCalls(reply, data.tool_calls, data.research);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    if (!reset) $(".connection").textContent = "OpenAI · 응답 수신 완료";
    renderContext(data);
    $("#message").value = "";
  } catch (error) {
    renderCriteria(previousHorizon, previousFocus, previousCoverage);
    addMessage(error.message, "error");
    refreshStatus();
  } finally {
    busy = false;
    document
      .querySelectorAll("button")
      .forEach((button) => (button.disabled = false));
    $("#composer-status").textContent = "종목 · 투자 관점 · 궁금한 점";
    $("#message").focus();
  }
}

$("#chat-form").addEventListener("submit", (event) => {
  // 선택한 관점이 있고 사용자가 새 관점을 쓰지 않았으면 입력에 관점을 덧붙인다.
  event.preventDefault();
  let message = $("#message").value.trim();
  if (message && selectedHorizon && !/(단기|중기|중장기|장기)/.test(message))
    message += ` (${selectedHorizon} 관점)`;
  send(message);
});
$("#message").addEventListener("keydown", (event) => {
  // Enter는 전송, Shift+Enter는 줄바꿈. 한국어 IME 조합 중 Enter는 전송하지 않는다.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
document
  .querySelectorAll("[data-query]")
  .forEach((button) =>
    button.addEventListener("click", () => send(button.dataset.query)),
  );
document
  .querySelectorAll("[data-horizon]")
  .forEach((button) =>
    button.addEventListener("click", () => send(button.dataset.horizon)),
  );
$("#new-chat").addEventListener("click", () => send("/reset", true));

icons();
// 클릭 바깥/ESC로 도움말을 닫는다. DOM 요소가 아닌 설명 문자열에 이벤트를 연결하지 않는다.
document.addEventListener("click", (event) => {
  const help = $("#criteria-help");
  if (help && !help.contains(event.target)) help.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#criteria-help")?.open) {
    $("#criteria-help").open = false;
    $("#criteria-help summary").focus();
  }
});
const statusReady = refreshStatus();
// 첫 화면 추천은 서버가 수집한 관측값이다. LLM이나 고정 종목 목록으로 생성하지 않는다.
async function loadDiscovery() {
  const root = $("#discovery");
  if (!root) return;
  try {
    const response = await fetch("/api/discovery");
    if (!response.ok) throw new Error("discovery");
    const data = await response.json();
    root.replaceChildren();
    for (const [key, label] of [["news", "뉴스 주목 종목"], ["active", "거래량 상위 종목"]]) {
      const heading = document.createElement("h3");
      heading.textContent = label;
      root.append(heading);
      const list = document.createElement("div");
      list.className = "sample-queries";
      for (const row of data[key] || []) {
        const button = document.createElement("button");
        button.type = "button";
        const body = document.createElement("span");
        const name = document.createElement("strong");
        name.textContent = `${row.region === "kr" ? "한국" : "미국"} · ${row.stock.name} (${row.stock.code})`;
        const metric = document.createElement("small");
        metric.textContent = key === "news" ? `최근 24시간 RSS 표본 · ${row.count}개 제목에 등장` : `거래량 ${Number(row.volume).toLocaleString("ko-KR")}주`;
        const detail = document.createElement("small");
        detail.textContent = key === "news" ? row.article.title : `${row.source} · ${new Date(row.as_of).toLocaleString("ko-KR")}`;
        body.append(name, metric, detail);
        const arrow = document.createElement("i");
        arrow.dataset.lucide = "arrow-up-right";
        button.append(body, arrow);
        button.addEventListener("click", () => send(`${row.stock.name} (${row.stock.code}, ${row.stock.exchange}) 어때?`));
        list.append(button);
      }
      root.append(list);
      if ((data[key] || []).length < 2) {
        const empty = document.createElement("p");
        empty.textContent = "일부 시장의 후보를 확인하지 못했습니다.";
        root.append(empty);
      }
    }
    const note = document.createElement("p");
    note.textContent = `한국·미국 각 1개 · 매수 추천 아님 · 거래량은 최근 거래 시점 기준 · ${new Date(data.fetched_at).toLocaleString("ko-KR")} 갱신`;
    root.append(note);
    icons();
  } catch {
    root.textContent = "관심 종목을 불러오지 못했습니다. 아래에서 종목을 직접 질문할 수 있습니다.";
  }
}
loadDiscovery();
// 최초 기준표 로딩 Promise를 보관해, 로딩 직후 빠르게 관점을 선택해도 순서를 보장한다.

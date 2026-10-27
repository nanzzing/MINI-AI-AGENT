# MINI-AI-AGENT

A small Python project for applying LLM prompt engineering concepts.

## Requirements

Python 3.11 or later.

## Setup (PowerShell)

```powershell
cd C:\GIT\MINI-AI-AGENT
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\mini-ai-agent.exe
```

## Structure

```text
src/mini_ai_agent/  Python application package
.env.example       Environment variable template
pyproject.toml     Package metadata and dependencies
```

## Web UI

### Render demo deployment

Connect the GitHub repository as a Python Web Service, branch `master`, root
directory blank, Free instance. Build: `pip install .`. Start:
`python -m mini_ai_agent.server --host 0.0.0.0`. Render supplies `PORT`.
Alternatively, use the included `render.yaml` as a Blueprint.

Do not upload `.env` or `.venv`. For the first public smoke test, leave the API
key unset: charts work, paid AI requests are disabled. Before enabling
`OPENAI_API_KEY` in Render's environment settings, add authentication and request
limits. The current stdlib HTTP server is a prototype, not a hardened production
server. Sessions are memory-only and disappear on restart or free-tier sleep.
Successful local tests do not guarantee that Yahoo permits queries from Render;
verify stock data after deployment. No deployment or Git push is automatic here.

```powershell
cd C:\GIT\MINI-AI-AGENT
.\.venv\Scripts\python.exe -m mini_ai_agent.server --port 8765
```

Open http://127.0.0.1:8765. The web UI uses the same conversation rules as
the terminal. Conversations are held in server memory and reset on restart.
The chart displays actual adjusted daily OHLCV from Yahoo Finance, not streaming
quotes. Chat uses OpenAI Responses API. Stock search also works without an API key.

### Research wait time

Model rounds and tool executions no longer have a fixed per-question count cap.
The Agent Loop continues until a valid final answer or an API/network error;
context, tool permissions, required data, and answer validation still apply.
Network timeouts remain (90 seconds for the subscription client, 45 for API-key
requests); these are not deadlines for the entire investigation. Repeated calls
can consume more usage. For an unwanted ongoing loop, stop the local server
process. Closing the page is not immediate cancellation: the server stops further
calls when a subsequent progress write detects disconnection, not during an
already-running model or data request.

The chat now shows actual server stages (model round, tool execution, validation),
total elapsed seconds, and time in the current stage. Expand the live execution
record while waiting. Completed model/tool durations remain in the final Agent
Loop record, and received progress remains visible after an error. These are
observable execution events, not the model's private reasoning or a completion
estimate. This adds visibility; it does not change model effort or parallelize tools.

The browser requests `Accept: application/x-ndjson` on `POST /api/chat` to receive
`progress`, `result`, or `error` events. The final result is still validated before
being displayed; a disconnected stream without a result is an error. Clients
without this Accept header retain the original JSON response contract.

### Research checklist and common rules

`conversation.py` defines 16 short-, 12 medium-, and 20 long-term criteria.
The medium-term monthly filter starts with the latest completed month and requires
at least three consecutive qualifying months. `prompts/research.txt` separately
holds the eight common response rules, including source/date disclosure, opposing
evidence, explicit assumptions, and the short-term 20/40-session indicator window.

`coverage.py` maps criterion names to evidence explicitly rather than zipping
parallel lists by index. New checklist names do not create new data providers.

### Long-term valuation tool

`get_valuation_comparison` is offered and executable only after long-term context
is confirmed. Long-term answers require a valid cached result or an attempted
lookup; missing provider fields do not cause repeated requests. Results are cached
by stock and exchange for five minutes and removed from model context when moving
to short/medium research. The eval fixture replaces this provider too.

For US stocks, `valuation.py` reads Yahoo Finance company trailing/forward P/E, P/B, ROE and
EV/EBITDA when supplied. FCF yield uses provider free cash flow / market cap only
when both currencies match. Missing/non-finite values are not zero; nonpositive
valuation multiples are unavailable, while negative ROE/FCF remain valid data.
Provider periods are explicitly marked unknown where unverified. Quote time is
not the earnings/estimate date. No adjusted chart prices are used for these ratios.

For Korean stocks, `korean_valuation.py` calculates TTM earnings P/E, latest
book-value P/B and TTM ROE from OpenDART. It never calls Yahoo `get_info`.
P/E sums parent-attributable profit for the latest four consecutive standalone
quarters, anchored at the latest reported quarter. Missing/duplicate quarters,
mixed currencies/scopes or missing parent profit block P/E; it never substitutes
older quarters or annual earnings. Q4 is already derived by the DART adapter
as annual minus nine-month cumulative income, and is not differenced again. ROE
uses the same TTM profit divided by average parent equity at the latest quarter
end and exactly one year earlier. Missing, duplicate or mixed-basis equity blocks
ROE; annual equity can only fill an exact December-end match. Annual financial
history remains available separately for long-term trend analysis. Separate
statements use separate net income/equity; CFS/OFS are never mixed. Nonpositive
profit blocks P/E, nonpositive equity blocks P/B/ROE, but negative ROE is valid.

P/E and P/B are **whole-company estimates**, not common-share EPS/BPS multiples:
sum each share class's recent dividend-unadjusted closing price times its DART
report-date outstanding shares (excluding treasury stock). Share counts are not
live: later issuance, retirement and treasury changes are unverified. Price dates
must match across classes; splits/mergers after the share-count date block these
estimates. Prices still come from Yahoo's chart API, so price access can fail
independently while DART-only ROE remains available.

`data/kr_share_classes.json` contains verified multi-class ticker mappings with
an issuer source URL (initially Samsung common/preferred). Other issuers with only
ordinary shares need no extra mapping. Unmapped preferred/multiple classes are
not priced as ordinary shares: add an independently verified mapping before use.
Numbers are fetched, never hardcoded. Forward P/E, EV/EBITDA and FCF yield are
currently unavailable on this Korean path rather than falling back to Yahoo
financial estimates. Formula, inputs, periods and filing links accompany metrics.

Industry averages (including forward P/E), peer comparisons and historical
valuation ranges are **not connected yet**. The tool returns explicit unavailable
sections, not invented benchmarks, and the prompt prohibits substituting company
metrics for industry averages. A licensed comparison provider with compatible
periods and aggregation definitions is needed to complete those comparisons.
Public redistribution of Yahoo/other provider data requires checking their terms.

### Always-visible US market indicators

The sticky top strip loads `/api/market-indicator?name=vix|buffett|fear_greed`
independently from chat. It does not call the model or add these values to its
context. Each provider has a five-minute, per-process cache, including failures;
hidden tabs do not poll. Failed refreshes retain any cached value with a stale
label and its original observation date, never a fabricated zero.

- VIX: Cboe `VIX_History.csv`, latest available daily close, not a live quote.
- Buffett proxy: FRED `NCBEILQ027S / (GDP * 1000) * 100`, using the latest common
  quarter. The numerator is nonfinancial corporate equity value in millions of
  USD, not total listed market capitalization; GDP is billions of USD at an
  annual rate. The visible proxy label and formula disclosure are intentional.
- CNN stock Fear & Greed: OANOR `/stockfeargreed-api/v1/index` gateway.
  Set `OANOR_API_KEY` in local `.env` (environment variables take precedence).
  Subscribe to the corresponding API plan in OANOR; a key alone may not suffice.
  Restart the Python server after deploying this change. Configuration changes
  can otherwise take up to five minutes to outlive the response cache.
  The key is sent server-side in `x-oanor-key`, never in URLs or browser payloads;
  redirects are rejected. The tile shows score, rating and upstream `updated`
  date, with CNN and OANOR links. Missing key, authentication, subscription and
  quota failures get safe messages. Failed refreshes preserve the prior value.
  Direct CNN fetching is no longer used. No crypto substitution is made.
  Unit/browser tests use mock responses; real freshness and account access must
  be verified after adding a key. Published quotas and redistribution rights
  must be checked against the selected provider plan before public deployment.

Review provider redistribution/licensing terms before public deployment. Source
links and reference periods remain visible. Tests: `tests/test_market_indicators.py`
and `tests/browser_market_indicators.cjs` (mocked browser responses).
Unavailable benchmarks, consensus, segment disclosures, ATR, and other missing
components are disclosed in the evidence bank and the UI's data limitations.
Some criteria have usable partial inputs, not complete coverage of every component.

## OpenAI Connection

### Local ChatGPT subscription (Plus / Pro)

The local web server now defaults to **Sign in with ChatGPT**:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m mini_ai_agent.server --host 127.0.0.1 --port 8765 --auth chatgpt
```

Open http://127.0.0.1:8765, click **Continue with ChatGPT**, sign in on OpenAI's
official page, and allow ChatGPT plan usage. The app loads the models available
to that account; choose one before chatting. Google sign-in alone is not a grant
to use the plan. Login cancellation, eligibility errors, or usage limits never
fall back to paid API requests. The account's actual eligibility is checked by OpenAI.

The existing stock tools, Agent Loop, evidence linking, and research criteria
remain in Python. The subscription adapter streams Responses with `store=false`,
wraps functions in a namespace, and omits the unsupported `max_output_tokens`.
Only a completed stream is accepted as a response. Model names are discovered,
not copied from `OPENAI_MODEL`. Live research/evaluation consumes plan usage.

Credentials are **not** stored in this repository, browser localStorage, or logs.
On Windows the app uses DPAPI under `%LOCALAPPDATA%\MINI-AI-AGENT\chatgpt`;
on other local systems it uses owner-only files under `~/.config/MINI-AI-AGENT/chatgpt`.
This is a new app connection, not a copy of Codex/browser credentials. Refreshes
are serialized with a file lock. Logout attempts remote revocation, clears local
tokens, and keeps the registration/host ID for later sign-in. Account or model
switching starts a new research session. Manage limits/disconnect in
https://chatgpt.com/settings/usage and ChatGPT security settings.

Subscription mode is restricted to loopback (`127.0.0.1`), rejects cross-origin
POSTs and invalid Host/CSRF values, and is disabled on Render/public bindings.
Do not expose it via a tunnel or reverse proxy. Remotely hosted or paid apps need
separate approval; this implementation is for personal local study.

Official integration contract:
https://developers.openai.com/siwc/token-sharing-open-source

### Separate API billing (existing deployment / CLI)

Use `--auth api` to explicitly run the local web UI with the existing API-key
path. Render/public binding and CLI/evals retain their existing API-key behavior;
`evals/run.py --live` still uses API billing, not this web login.

Edit the project-root `.env` locally:

```dotenv
OPENAI_API_KEY=aaa
OPENAI_MODEL=gpt-4.1-mini
```

Replace `aaa` with a valid OpenAI API key. The placeholder never sends a request.
The server reads `.env` on every message, so changing the key does not require a
restart. Process environment variables take precedence. Keys stay on the server.
The API uses separate API billing, not ChatGPT subscription usage.

Edit `src/mini_ai_agent/prompts/research.txt` to change the assistant instructions.
The selected investment criteria from `conversation.py` are also supplied.
The model selects `search_stock`, `set_research_context`, and `get_financials` tools. Python executes
them, and their outputs are returned to the model until a text answer is received.
The last successful conversation history is kept per browser session. Reset clears it.
Failures do not commit partial history or partially changed research context.
Once a stock and horizon are selected, Python always attempts news retrieval for
each successful research turn. Financial statements are fetched on demand by the
model or the financial download button. Calendar queries run only for short-term research.

## Research Data Policy

| Horizon | Financial request | News lookback | Earnings calendar |
| --- | --- | --- | --- |
| Short | Latest 4 quarters | 7 days | Next 30 days |
| Medium | Latest 3 annual periods + 8 quarters | 30 days | Not queried |
| Long | Latest 5 annual periods + 8 quarters | 90 days | Not queried |

`market_data.py` defines these policies. Financial coverage includes income,
balance sheet, and cash flow. Provider shortfalls are marked `partial` with the
actual period counts; missing values are never replaced with zero. Amounts use
the reported currency's base unit, not millions; EPS, ratios and shares retain
their respective units. Period dates are fiscal period ends, not publication dates.

Sources: Yahoo Finance via yfinance (unofficial, personal study use; review provider
terms before deployment), Google News RSS (latest 10 matching headlines, not article
bodies), and the existing KIS stock directory. Prices cache for 5 minutes and
financials for 1 hour; news is attempted each research turn. Data can be delayed or
unavailable. KONEX price mapping is unsupported. Earnings dates are estimates;
other scheduled disclosures are not yet supported. No automatic trading is provided.

Charts use vendored Lightweight Charts 4.2.3 with TradingView attribution. Daily
history is 2 years for short-term and 5 years for medium/long-term research. Monthly
candles aggregate daily data; the current month is incomplete. `technicals.py`
calculates SMA 20/60/120/200, alignment, 5-session slopes, price distances,
5/20/60/120/252-session returns, Wilder RSI14, EMA MACD(12,26,9), Bollinger(20,2),
volume ratio, annualized 20-session volatility and prior-20-session high/low proxies.
It also checks three completed monthly closes strictly above their 5-month SMA.
The current exchange-local day and current month are excluded from indicator
calculations. Missing history returns null, not a fabricated result. Full history
is calculated in Python; concise results are sent automatically to the model.
Market/sector relative performance still requires a separate comparison dataset.

## Next Steps

The conversation searches the downloaded Korean and US stock directory by
name or code, asks for a short, medium, or long investment horizon, and preserves
that context for the next reply. It then lists the research focus for that
horizon. Use `/reset` for a new conversation and `/exit` to quit.

Run the CLI or tests after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m mini_ai_agent.main
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

API integration tests
use simulated responses and do not require or spend a real API key.

## Stock Directory

`src/mini_ai_agent/data/stocks.json` contains names, codes, and exchanges,
plus download time and source URLs. Source: Korea Investment & Securities
official stock masters (https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info).
Coverage follows the provider's directory, not a guarantee of every listed security.
Includes KOSPI, KOSDAQ, KONEX, NASDAQ, NYSE, and NYSE American stock records.
ETFs/ETNs and other non-stock security groups are excluded. US names use the
provider's Korean display name when available. No prices or financials are stored.

Refresh manually and restart the server to load the updated directory:

```powershell
$env:PYTHONPATH = "src"
py -m mini_ai_agent.stocks
```

Refresh replaces the local snapshot only after all six downloads and validation
succeed. No API key is needed for these public master files.
Keep actual API keys in a local `.env` file, never in source code.

## Automatic Question Mode

The composer defaults to automatic mode, separate from the last stock's investment
horizon. The model selects industry, compare, explain, concept, focused, or research
with `set_response_mode`. Full stock research keeps its existing horizon checklist.
`search_topic_news` searches topic headlines without requiring a stock.
`get_company_data` fetches one verified company's requested dataset and scope without
changing the active stock. Comparison datasets are keyed by identity, kind and scope,
limited to 12 entries, and reusable for five minutes. General answers display their
queried evidence instead of the previous stock's checklist. News tools return titles,
not article bodies or verified guidance; missing information must remain explicit.
Run `node tests/browser_auto.cjs` against the local server (default port 8766) to
check the selector and follow-up behavior with mocked responses and no model cost.

## Stock Search Fallback

Chat stock resolution is model-driven: interpret a nickname or typo, call
`search_stock` with the likely company name or ticker, inspect the returned
candidates, then call `set_research_context`. New identities must have been
returned by search in this conversation; the shared catalog alone does not
authorize them. Search candidates survive clarification turns and are cleared
on conversation reset. Follow-up questions can reuse the current verified stock.
The prompt asks for clarification when the company or share class is ambiguous,
and limits alternate spelling searches instead of guessing indefinitely.
Typo interpretation depends on the model; a verified ticker does not by itself
prove that the model correctly understood the user's intended company.

Explicit `search_stock` and chart searches use the local KIS catalog first, then
Yahoo Finance Search on a miss. Ordinary chat context detection remains local.
Whitespace in company names and US class ticker separators (`BRK/B`, `BRK.B`,
`BRK-B`) are normalized. Partial company names return candidates, not an arbitrary
share class. If a Korean transliteration is not found, the model can search an
English name or ticker, but must confirm the returned identity before research.

External results are restricted to EQUITY and supported Korean/US exchanges;
ETFs, unsupported exchanges and malformed symbols are excluded. Verified
candidates enter a bounded in-memory directory for 24 hours, shared by tool
context validation and chart/financial lookup. Search responses cache for ten
minutes (failures: 30 seconds); no key or full-catalog download is needed at
startup. Restarting clears discoveries. Yahoo may reject requests from hosting
networks; a search failure is reported separately from no candidates. Discovery
does not guarantee prices or financials exist for the instrument.

## Korean Financials: OpenDART

KOSPI/KOSDAQ/KONEX financial statements now use OpenDART. US financials remain
on Yahoo. Prices/charts remain on Yahoo. Korean long-term valuation now calculates
P/E, P/B and ROE from DART and prices as described above; forward PER and industry
benchmarks remain unavailable.

Put your issued key in the project-root `.env` for local use:

```dotenv
DART_API_KEY=your-issued-key
```

On Render, add the same variable name in Environment and redeploy. Never commit
the actual key. Environment values override `.env`; `.env.example` is not read
as configuration. Restart the local server after changing code/configuration.
No key is required in the browser, no extra package is needed, and no LLM is
called to fetch statements. Missing/invalid keys, IP restrictions, quota and
maintenance failures produce explicit errors; there is no silent Yahoo fallback.

- Short: latest 4 quarters. Medium: 3 annual periods + 8 quarters. Long: 5 annual
  periods + 8 quarters. Latest means available filings, not future projections.
- The official corporation-code ZIP maps the exact stock code to a DART corp
  code. Company/catalog cache: 24 hours; reports: one hour, shared across horizons.
  Preferred shares without an exact mapping are not guessed from another ticker.
- This first adapter supports KRW standard accounts for **December year-end**
  companies. Other fiscal months, unverified currencies, ambiguous/unsupported
  custom accounts and historical fiscal-year changes are not guessed.
- Consolidated (CFS) first; separate (OFS) only on API no-data code 013. Every
  returned period retains the scope and original filing links.
- Interim income uses DART's three-month amount. Q2/Q3/Q4 cash flow uses the
  difference between same-year cumulative filings; Q4 income is annual minus
  nine-month cumulative. Scope mismatch or missing prior filings leaves a gap.
  Restatement differences between filings may affect these derived quarters.
  EPS is never derived by subtracting annual and nine-month EPS.
- FCF is operating cash flow minus gross PPE and intangible purchase outflows,
  only when both outflows are available. Missing amounts are not zero.
  Total liabilities are not interest-bearing debt; total equity and parent equity
  are distinct. No automated market valuation or forecast EPS is added here.
- Safe logs: `dart_request_failure` (endpoint/status/type, no URL or key) and
  `dart_api_failure` (DART status code). Per-request timeout 15 seconds, overall
  collection budget 90 seconds checked between requests. Completed periods are
  retained as partial data if later requests fail.

Official references: [whole statements](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020),
[corporation codes](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018),
[company profile / fiscal month](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019002).

## Yahoo diagnostics: local vs Render

Run the same symbol on both hosts, near the same time. Deploy this code before
running the Render command. This reads Yahoo only; it does not call the LLM.

Local PowerShell, from the project root:

```powershell
.\.venv\Scripts\python.exe -m mini_ai_agent.yahoo_diagnostics --symbol 005930.KS
```

Render Shell (not your local terminal):

```sh
python -m mini_ai_agent.yahoo_diagnostics --symbol 005930.KS
```

Startup diagnosis is disabled by default on both local and Render servers to
avoid extra Yahoo requests. Manual CLI diagnosis remains available. If Shell is
unavailable and diagnosis is needed again, explicitly set
`YAHOO_DIAGNOSTICS_ON_START=1` in Render Environment and redeploy. Keep the
existing Start Command. This runs a background diagnosis for `005930.KS` once
per process startup, including service wake-ups. Search Render Logs for
`yahoo_diagnostic`: start, environment, eight operation results, then complete.
Web serving is not blocked; each probe has its own subprocess and a 45-second
deadline. After collecting the logs, set `YAHOO_DIAGNOSTICS_ON_START=0` and
restart/redeploy to disable it. Explicit `1` overrides the disabled default.
No LLM requests or public diagnostic endpoint.

Without `--probe`, all eight checks run sequentially (up to about six minutes).
Other probe names: history,
yearly_income, yearly_balance, yearly_cashflow, quarterly_income,
quarterly_balance, quarterly_cashflow.

Compare `yahoo_diagnostic_environment` first: Python, OS and package versions.
Each `yahoo_diagnostic` reports the symbol, operation, duration, field/row counts,
missing valuation fields, observed HTTP error codes and safe error categories.
`ok` means returned data, not independently verified financial correctness.
`partial` means some requested info fields are missing; `empty` means no usable
data; `error` means an exception; `timeout` means the 45-second probe deadline.
An HTTP 401 with final `ok` may be a recovered intermediate request failure.
No observed HTTP code does not prove no HTTP error occurred: yfinance may hide it.

Each probe runs in a separate process without application caches; yfinance's
existing on-disk cookie cache is left intact. No keys, cookie/crumb values, raw
response bodies, raw exception text, proxy addresses or account details are
included in the report. Do not enable yfinance debug logging to share results.
This diagnoses failure scope, not Yahoo's private IP-blocking decisions.

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

## OpenAI Connection

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

# Master AI Brain Architecture: Three-Path Execution Model

This document serves as the absolute "Truth Engine" for the Stock Tool system design. It outlines the decoupled, asynchronous architecture separating hot-path execution, warm-path technical calculations, and cold-path AI sentiment analysis using Gemini 3.1 Flash-Lite.

---

## 1. The Core Paradigm: Decoupled Loops

Since LLMs are constrained by inference speeds (1–3 seconds minimum, and in practice up to ~3 minutes for a large structured-output call — see §2 Gemini Call Reliability), they cannot live in the execution loops of high-frequency or retail intraday trading (which require sub-second executions). The engine is strictly divided into three asynchronous paths with bounded latency budgets.

```
+--------------------------------------------------------------------------------+
|                                    HOT PATH                                    |
|   [Upstox Feed] ---> [In-Memory Price Cache] ---> [SL / TP Exit Engine]        |
|   Latency: <10ms | Database: None | I/O: None | LLM: None                       |
|   Gated by: MarketFeed.is_feed_healthy() — see §3a                             |
+--------------------------------------------------------------------------------+
                                       |
                                       | (Async Queue Ticks)
                                       ▼
+--------------------------------------------------------------------------------+
|                                   WARM PATH                                    |
|   [Tick Queue] ---> [talipp Indicator Engine] ---> [Baseline Buy/Sell Signals] |
|   Latency: 10-100ms | Calculations: RSI, MACD                                  |
+--------------------------------------------------------------------------------+
                                       |
                     (Approved Trade Gate Check + Conviction)
                                       ▼
+--------------------------------------------------------------------------------+
|                                   COLD PATH                                    |
|   [yfinance RSS + Custom Indian Portals] ---> [gemini-3.1-flash-lite]          |
|   Latency: 1-15s (typ.), up to ~3min at 40-symbol watchlist size               |
|   Outputs: Conviction Score, Action Gate (ALLOW/VETO), per-symbol reasoning    |
+--------------------------------------------------------------------------------+
```

---

## 2. Path Detail Specifications

### 🟥 HOT PATH (Execution Core & Exits)
*   **Latency Budget:** < 10ms.
*   **Role:** Real-time quote streaming and emergency position exits.
*   **Safety Rule:** This loop must never run blockages. No print logs to disk, no file reads, no external API calls, and no LLMs.
*   **Exits:** It continuously cross-checks active database positions (loaded in-memory on startup) against the price cache. If a stop-loss (SL) or target-profit (TP) threshold is crossed, it fires an exit order instantly — **unless the feed is currently unhealthy or in its post-recovery grace window** (§3a), in which case the check is skipped entirely for that tick rather than acting on an unreliable price.

### 🟨 WARM PATH (Technical Calculations)
*   **Latency Budget:** 10–100ms.
*   **Role:** Processes ticks to update technical indicators and generate signals.
*   **Engine:** The `MarketFeed` aggregates incoming 1-second ticks into 5-minute OHLC candles (`Config.CANDLE_PERIOD_SECONDS`). It emits only the closed candle to the Warm Path. Uses the `talipp` library for RSI(14) and MACD(12,26,9), updated on each closed candle.
*   **Signal Logic (`src/technicals.py`):** BUY/SELL confirmation uses **OR** logic, not a strict AND of both indicators — an RSI extreme alone is enough to trigger, and a MACD crossover on the "correct half" of the RSI range can trigger without RSI itself being extreme:
    *   **BUY:** `RSI < 40` **OR** (`MACD line > MACD signal` **AND** `RSI < 50`)
    *   **SELL:** `RSI > 60` **OR** (`MACD line < MACD signal` **AND** `RSI > 50`)
    *   *History:* the original rule required RSI beyond 35/65 **AND** a simultaneous MACD crossover in the same direction. Across a real trading day this combination was so rare it produced almost zero fireable signals — confirmed empirically via a 5-hour live run that generated technical signals on only a handful of the 40 watchlist symbols and executed zero trades, because the AI gate (refreshed every 300s) essentially never lined up with a signal (refreshed per-tick) at the same instant. The OR logic + loosened bands were introduced specifically to fix this.

### 🟦 COLD PATH (The AI Sentiment Gate & Safety Guardrails)
*   **Latency Budget:** 1–15 seconds typical; can run to ~2–3 minutes for a single Gemini call once the watchlist reached 40 symbols (large structured-output response). This is expected, not a hang — see **Gemini Call Reliability** below.
*   **Role:** An asynchronous risk-veto and macro-guardrail gate driven by a multi-agent LLM pipeline.
*   **Market-Hours Gating:** The Cold Path loop calls `Config.is_market_hours()` on every cycle. Outside NSE trading hours (09:15–15:20 IST, weekdays; always "open" in VIRTUAL mode) it skips the Gemini call entirely, forces all gates to `VETO`, and sleeps 60s before rechecking — there is no reason to burn API quota analyzing symbols that cannot execute a trade anyway.
*   **Strategy Clarity (Single-Shot Committee Agent):** The AI acts as a sophisticated, slow-moving filter for trades generated by the Warm Path. The final gatekeeper is a context-aware LLM. A single `CommitteeAgent` call plays the SentimentAnalyst / MacroAnalyst / RiskGatekeeper roles in one prompt. Every 5 minutes (when the market is open) it evaluates:
    1.  **Macro & Sentiment Data:** Raw headlines fetched by the `NewsAgent` (yfinance, fetched **in parallel** via `asyncio.gather` across all watchlist symbols — a prior sequential per-symbol loop was adding 60+ seconds to every cycle before Gemini was even called) plus Livemint macro RSS, scored inside the single committee prompt.
    2.  **Financial State:** Real-time injection of available account capital and open position states.
    3.  **Technical Trends:** Real-time injection of the trailing 10 RSI and MACD values for momentum awareness.
    4.  **Fraud Check:** The deterministic `FraudAgent` scans symbol news for India-specific governance red flags (SEBI penalty/ban, RBI action, ED raid, promoter pledge, NPA, NCLT/IBC, auditor resignation, forensic audit, etc.) and passes a boolean flag into the committee prompt to isolate compromised stocks.
    > **Note:** An earlier `EarningsAgent` (PEAD via `yfinance.earnings_dates`) was removed — yfinance does not reliably return future/recent earnings for NSE `.NS` tickers, and PEAD is a multi-week drift signal that is meaningless on a 5-minute intraday horizon.
*   **Prompt Bias (participation vs. caution):** The committee prompt explicitly instructs the model to **default to `ALLOW_ALL`** unless there is a concrete, symbol-specific reason not to trade — a quiet news day is not itself a reason to veto, and low available cash is no longer grounds for VETO except when genuinely near zero. This was tightened after observing the model default to VETO/neutral far too often on an account with a large (Rs. 3,00,000) capital base where cautious-by-default behavior meant the bot effectively never traded.
*   **Gemini Call Reliability (hard timeout):** `asyncio.wait_for()` wrapped around `asyncio.to_thread()` **cannot** actually interrupt a blocking SDK call once it is running inside a worker thread — asyncio cancellation is cooperative, and synchronous code has no cancellation points. The installed `google-genai` SDK also has no working HTTP-level timeout of its own (its `_api_client.py` hardcodes `timeout=None` pending an upstream TODO). `call_gemini_with_retry()` therefore submits the Gemini call to a dedicated `concurrent.futures.ThreadPoolExecutor` and waits on `Future.result(timeout=180)`, which genuinely stops waiting after the deadline regardless of whether the submitted call has finished (the orphaned call keeps running in the background; its result is simply discarded). This is a real safety net — before this fix, a single stuck Gemini call could freeze the entire Cold Path indefinitely, leaving every gate at whatever it was last set to forever.
*   **Gemini Quota:** One `CommitteeAgent` call per 5-minute cycle covers the entire watchlist in a single request. A token-bucket `GeminiRateLimiter` caps calls at 12/60s and retries on 429.
*   **Data Aggregators:**
    *   **`yfinance`:** Free ticker-based news headlines (e.g. `SBIN.NS`).
    *   **Livemint Markets RSS:** Free macro market sentiment news fetched asynchronously using `aiohttp` and `feedparser`.
*   **AI Engine:** Queries `gemini-3.1-flash-lite`. Returns a structured JSON output enforcing a strict schema (`ALLOW_BUY`, `ALLOW_SELL`, `ALLOW_ALL`, `VETO`, or `FLATTEN`), plus a free-text `reasoning` string per symbol. `CommitteeAgent.run()` returns `(gates, reasoning)` — `BrainEngine` stores the reasoning dict on `self.reasoning`, keyed by base symbol, so `main.py` can attach the AI's stated rationale to any trade it places (see §4 `orders` table / §6 Dashboard).
*   **FLATTEN Safety Switch:** If the `CommitteeAgent` detects a major negative catalyst (e.g., fraud flag, regulatory bans, macro crashes), it returns a `FLATTEN` gate. In the next evaluator tick, `main.py` immediately intercepts this status, bypasses regular entry loops, and calls `flatten_positions_for_symbol` to place emergency market exit orders for any active holdings on that ticker — subject to the same feed-health gate as everything else in the execution loop (§3a).

---

## 3. Regulatory Compliance & Network Architecture

### SEBI Algorithmic Trading Guidelines (February 4, 2025 Updates)
Under SEBI guidelines, retail algorithmic trading via APIs must incorporate strict risk-management and system safeguards to ensure market orderliness:
1. **Pre-Trade Risk Management:** Our engine implements automated capital limits (`MAX_CAPITAL_PER_TRADE`) and maximum loss limits (`MAX_DAILY_LOSS`) enforced at the database level.
2. **Order Placement Cooldowns:** A per-symbol cooldown (currently 120 seconds — see §5 for history) is enforced to prevent runaway execution loops or rapid order spamming.
3. **Execution Compliance:** Automatic target profits (2%) and stop losses (1%) are monitored continuously in-memory to prevent open-ended exposure — gated by feed health (§3a).
4. **Timezone-Aware Market Hours Guard:** `Config.is_market_hours()` is the single source of truth for "is the market open" and is used by both `main.py` (trade execution) and `brain.py` (Gemini calls). It restricts active order placement and AI evaluation to official market hours (09:15 to 15:20 IST) on weekdays. In VIRTUAL mode, this guard always returns `True` for 24/7 demo/testing.
5. **Close-Market Auto-Flatten (15:25 IST):** At 15:25 IST, a background task automatically suspends trading, cancels open virtual orders, and flattens all positions to prevent overnight carrying risk (valid for PAPER/LIVE modes; VIRTUAL mode instead runs a fixed `VIRTUAL_RUNTIME_HOURS` timer).

### 3a. Feed-Health Guard (Stale-Data Protection)

Added after a real incident: the Upstox access token expired mid-session, every REST poll started failing with `401 Invalid token`, the in-memory price cache silently froze at its last successful value, and the bot **opened a position against that frozen price** (a flat, unmoving price can itself look like an RSI/MACD extreme). The instant a fresh token was supplied and real prices resumed, the resulting gap-jump from the stale value to the true live price **immediately tripped that position's stop-loss/target**, because `monitor_open_positions` checked prices every 100ms with no concept of "is this price actually current right now."

**`MarketFeed.is_feed_healthy()`** (`src/market_feed.py`) closes this gap:
*   Tracks `last_tick_time`, updated on every successful price update from any source (VIRTUAL simulator, WebSocket, or REST poll).
*   Returns **`False`** (stale) if no successful tick has landed in `FEED_STALE_AFTER_SECONDS` (10s — a few REST-poll cycles' worth of slack for transient hiccups, but fast enough to catch a real outage like an expired token).
*   Returns **`False`** for `FEED_RECOVERY_GRACE_SECONDS` (8s) immediately after a stale spell ends — the first fresh tick after a gap is a jump, not a signal, and needs a few confirmed-fresh ticks before it's trusted.
*   Always returns **`True`** in VIRTUAL mode (self-generated data, cannot go stale by definition).

This is checked in exactly two places, both of which `continue`/skip their entire evaluation for that cycle when unhealthy:
*   `main.py::automated_execution_loop` — no new entries, and the AI `FLATTEN` gate is not acted on, while the feed is unhealthy.
*   `orders.py::monitor_open_positions` — stop-loss/target checks are skipped entirely; existing positions simply aren't evaluated until the feed is proven fresh again, rather than being closed against an unreliable price.

Feed state is exposed on `/api/status` as `feed_healthy` and surfaced on the dashboard as a **FEED LIVE** / **FEED STALE — TRADING PAUSED** header badge (§6).

### 3b. Upstox Token Lifecycle (Operational Constraint)

Upstox access tokens are killed server-side every night at **03:30 IST**, regardless of when they were issued or whether the process holding them is still running. There is no refresh-token flow available on this API tier — a human must generate a fresh token daily at `account.upstox.com/developer/apps` and set it as `UPSTOX_ACCESS_TOKEN`. This is true **regardless of hosting location** (local machine or Railway) — moving the deployment to Railway removes the need to keep a laptop powered on, but does **not** remove the daily manual token-refresh step. A token generated in the few minutes immediately before 03:30 IST will die almost immediately (observed directly: a token issued at 03:28 IST expired at 03:30 IST, living about two minutes) — always generate the day's token *after* the reset window, not before it.

### Static IP Whitelisting Constraints
* **Mandatory Broker Whitelisting:** Upstox mandates that all order placement, modification, and cancellation requests originate from registered static IP addresses. Requests from dynamic or unregistered IPs are immediately blocked. (Not currently relevant in PAPER mode, where orders are virtual and only the read-only LTP/WebSocket endpoints are called — becomes relevant if/when `TRADING_MODE=LIVE` is used.)
* **Cloud NAT Gateway Routing:** When deploying in container environments (e.g. Railway or Render) that use rotating outbound IPs, LIVE-mode order routing to `api-hft.upstox.com` must go through a **Static IP Proxy** (such as QuotaGuard Static or an AWS NAT Gateway), registered in the Upstox Developer Console.

### WebSocket vs. REST Polling (Free-Tier Constraint)
Upstox's free (non-"Plus") API plan returns a valid WebSocket subscription acknowledgement but **never streams actual tick data** over it. `market_feed.py::_run_live_feed()` accounts for this: it attempts the WebSocket connection first, waits up to 30 seconds for a real tick to arrive, and if none does, cancels the WebSocket task and falls back to `_run_rest_poll_feed()`, which polls the `v2/market-quote/ltp` REST endpoint once per second for all watchlist instruments in a single batched request. In practice, on the current account tier, the system always ends up on REST polling.

---

## 4. Module Status

| Module | Status | Description |
|--------|--------|-------------|
| `src/main.py` | ✅ Stable | FastAPI app + orchestrator. Owns `automated_execution_loop` (signal × gate × feed-health matching, dynamic position sizing, cooldown, reasoning composition), `shutdown_scheduler` (EOD flatten), and all HTTP endpoints (`/api/status`, `/api/symbols`, `/portfolio`, `/prices`, `/execute`, `/backtest`, `/admin/reset-simulation`). |
| `src/config.py` | ✅ Stable | Single source of truth for risk limits, the watchlist (`SYMBOL_MAP`), and `is_market_hours()`. Loads from `.env` / process environment via `python-dotenv`. |
| `src/market_feed.py` | ✅ Stable | Singleton `MarketFeed`. VIRTUAL: random-walk simulator. PAPER/LIVE: Upstox WebSocket-with-REST-poll-fallback (see §3, WebSocket vs. REST Polling). Owns `is_feed_healthy()` (§3a). `INSTRUMENT_KEYS` and seed prices are derived from `Config.SYMBOL_MAP` rather than hardcoded, so this module cannot drift out of sync with the configured watchlist. |
| `src/technicals.py` | ✅ Stable | RSI(14) + MACD(12,26,9) via `talipp`. See §2 Warm Path for the current OR-based signal logic. |
| `src/brain.py` | ✅ Stable | `BrainEngine` (Cold Path orchestrator), `CommitteeAgent` (single-shot Gemini call), `NewsAgent`, `FraudAgent`. See §2 Cold Path for market-hours gating, the hard-timeout fix, and reasoning capture. |
| `src/orders.py` | ✅ Stable | PAPER/VIRTUAL: SQLite-backed virtual fills. LIVE: Upstox `/v3/order/place` HFT REST API. `RiskEngine` for deterministic Max Daily Loss and Consecutive Loss Circuit Breaker tracking (persisted across restarts). `monitor_open_positions` is gated by feed health (§3a). |
| `src/auth.py` | ✅ Stable | VIRTUAL: mock token. PAPER/LIVE: if `UPSTOX_ACCESS_TOKEN` is set in the environment, uses it directly (daily manual-refresh path, §3b); otherwise falls back to an interactive terminal OAuth flow — **this interactive fallback will hang forever on a non-interactive host like Railway**, so a valid `UPSTOX_ACCESS_TOKEN` must always be present before deploying there. |
| `src/db.py` | ✅ Stable | SQLite (WAL mode). All queries offloaded to `asyncio.to_thread()`. `orders` table carries `exit_price`, `pnl`, and `reasoning` columns (see below) in addition to the original entry fields; migrations are additive `ALTER TABLE ... ADD COLUMN` statements wrapped in try/except so they're safe to run against an existing populated database (verified against Railway's persistent volume). |
| `src/backtest.py` | ✅ Stable | Downloads real NSE OHLCV data from Yahoo Finance. Feeds tick-by-tick through `TechnicalEngine`. Reports Win Rate, P&L, trade-by-trade log, with realistic slippage (0.05%) and transaction charges. `SYMBOL_MAP` is imported directly from `Config.SYMBOL_MAP` (previously a third independent hardcoded copy — see §7). |
| `src/static/index.html` | ✅ Stable | Dashboard UI. Watchlist symbol metadata is fetched from `/api/symbols` at load time rather than hardcoded (§7). See §6 for the metrics/badges it renders. |

### `orders` Table Schema (Trade Record)

Beyond the original entry fields (`symbol`, `quantity`, `price` [entry], `target`, `stop_loss`, `order_type`, `transaction_type`, `status`, `timestamp`), the table also carries:
*   **`exit_price`** / **`pnl`** — populated when a position closes. Previously, closing a trade *overwrote* the `price` column with the exit price, destroying the entry price entirely; the Trade History UI could only show one ambiguous number. These are separate columns now, so entry and exit are both visible.
*   **`reasoning`** — a human-readable string composed at trade time in `main.py`, combining (a) which technical signal fired, (b) the AI committee's stated reasoning for that symbol this cycle, and (c) how the trade quantity was sized against the per-trade capital cap. Threaded through `place_order()` → `_insert_order_sync()`. Answers "why did the bot buy/sell this, and this many shares" directly from the trade record rather than requiring a separate lookup against whatever the AI's gate reasoning happened to be at some other point in time.

### Live Feed Authorization Flow (WebSocket attempt, before REST-poll fallback)

```
1. UPSTOX_ACCESS_TOKEN present in env  →  auth.py returns it directly (no OAuth flow)
2. feed.set_access_token(token)
3. GET  /v3/feed/market-data-feed/authorize  →  authorizedRedirectUri (wss://)
4. CONNECT wss://...   (binary protobuf stream)
5. SEND JSON: { method: "sub", data: { instrumentKeys: [...], mode: "full" } }
6. Wait up to 30s for a real tick...
   - Tick arrives  →  RECV binary frames  →  FeedResponse.ParseFromString()  →  price_cache update
   - No tick in 30s (current account tier)  →  cancel WS, switch to REST poll (§3)
```

### Backtesting CLI

```bash
python src/backtest.py --symbol SBIN.NS --interval 1h --period 1mo
python src/backtest.py --symbol ITC.NS  --interval 5m  --period 5d
```

Or trigger from the dashboard: **Backtest Lab** tab → select symbol/interval/period → **▶ Run Backtest**.

---

## 5. Financial Safety Configurations

*   **Max Daily Loss:** Realized P&L is checked at the database level. If losses exceed `MAX_DAILY_LOSS`, the app suspends all operations.
*   **Consecutive-Loss Circuit Breaker:** `RiskEngine` halts new trades after 3 consecutive losing trades. The counter is persisted to the `daily_stats` table and reloaded on startup, so a container restart mid-streak does not silently reset the breaker. It resets to 0 on any winning trade and naturally resets each new trading day.
*   **Max Capital Per Trade:** The value of a single trade (LTP × Quantity) cannot exceed `MAX_CAPITAL_PER_TRADE`. There is no separate share-quantity cap anywhere in the order path — position size is purely `min(MAX_CAPITAL_PER_TRADE, available_balance) / current_price`, so raising the capital limit directly produces larger share volumes.
*   **Per-Symbol Cooldown:** 120 seconds between orders on the same symbol (reduced from an original 300s to allow more trading opportunities per day once the signal logic was loosened — see §2 Warm Path history note).
*   **Watchlist:** 40 Nifty50 constituents individually tradeable within the current `MAX_CAPITAL_PER_TRADE`, defined in `Config.SYMBOL_MAP`. Instrument keys are verified against Upstox's public instrument master (`assets.upstox.com/market-quote/instruments/exchange/complete.json.gz`), not guessed — an earlier incident involved a watchlist member whose price had drifted above the per-trade cap, making it permanently untradeable without anyone noticing. 10 genuine Nifty50 members (MARUTI, ULTRACEMCO, BAJAJ-AUTO, APOLLOHOSP, EICHERMOT, DIVISLAB, BRITANNIA, HEROMOTOCO, TITAN, LT) are excluded because their share price exceeds even the current per-trade cap. TATAMOTORS maps to its post-2025-demerger passenger-vehicle entity (TMPV); LTIMINDTREE could not be located in the instrument master under any known symbol and was dropped rather than guessed.
*   **Current default values** (`src/config.py`, overridable via environment):
    *   `STARTING_CAPITAL = 300000.0` (Rs. 3,00,000)
    *   `MAX_DAILY_LOSS = 200000.0` (Rs. 2,00,000)
    *   `MAX_CAPITAL_PER_TRADE = 150000.0` (Rs. 1,50,000)
*   **Authentication (Manual):** Headless TOTP automation was removed for compliance reasons. PAPER/LIVE mode uses either a pre-set daily `UPSTOX_ACCESS_TOKEN` (fast path, required for any non-interactive host — §3b) or an interactive terminal OAuth flow.

**Railway Environment Variables (as currently deployed):**
```
STARTING_CAPITAL=300000.0
MAX_DAILY_LOSS=200000.0
MAX_CAPITAL_PER_TRADE=150000.0
TRADING_MODE=PAPER
CANDLE_PERIOD_SECONDS=300
UPSTOX_CLIENT_ID=...
UPSTOX_CLIENT_SECRET=...
UPSTOX_ACCESS_TOKEN=...   # manually refreshed daily — see §3b
GEMINI_API_KEY=...
```

---

## 6. Dashboard (Frontend)

Served from `src/static/index.html`, polling `/api/status`, `/prices`, and `/portfolio` every 1.5s.

*   **Header badges:** trading mode (VIRTUAL/PAPER/LIVE), **MARKET OPEN / MARKET CLOSED** (from `market_hours_open`), and **FEED LIVE / FEED STALE — TRADING PAUSED** (from `feed_healthy`, §3a) — so a stale price feed is visible at a glance instead of silently masquerading as normal (frozen-but-plausible-looking) prices.
*   **Wallet Balance card:** live available cash (`wallet_balance` on `/api/status` — `starting_capital + realized_pnl − capital locked in open positions`), shown as the first/most prominent metric, colored green/red against the starting balance.
*   **Market Watchlist & Signals table:** symbol, LTP, technical signal, AI gate — gate badges read "AI: BUY ALLOWED" / "AI: SELL ALLOWED" / "AI: NO TRADE (VETO)" / "AI: CLOSE POSITION NOW" (an earlier "AI: BUY GATED" wording for `ALLOW_BUY` was ambiguous — read as blocked when it meant the opposite).
*   **Open Positions / Trade History tables:** both include a **"Why"** column showing the recorded `reasoning` string for that trade (truncated, full text on hover). Trade History shows Entry Price, Exit Price, and Realized P&L as separate columns (§4, `orders` table schema).
*   **Symbol metadata (`SYMBOLS_MAP` in JS)** is fetched from `GET /api/symbols` at page load rather than hardcoded — a prior hardcoded copy in the page source drifted out of sync with the backend watchlist and kept displaying a symbol the backend had already dropped. The Backtest Lab symbol dropdown is populated from the same source.

---

## 7. Single Source of Truth: `Config.SYMBOL_MAP`

Historically, the watchlist existed as **three independent hardcoded copies**: `config.py` (canonical), `market_feed.py` (`INSTRUMENT_KEYS` / base prices), and `src/static/index.html` (`SYMBOLS_MAP` in JS). When the watchlist changed in `config.py` alone, the frontend kept showing a symbol the backend had already removed — with a price frozen at ₹0.00, since that instrument was no longer subscribed to on the feed. `market_feed.py` and `backtest.py` now derive their symbol lists directly from `Config.SYMBOL_MAP`, and the frontend fetches its copy from `/api/symbols` at load time. There is exactly one place to add or remove a watchlist symbol now: `Config.SYMBOL_MAP`.

---

## 8. Deployment & Database Persistence

The system is configured for cloud deployment to Railway to support 24/7 execution without requiring a personal computer to stay active.

*   **Two live environments, two different meanings of "live":**
    *   **Railway (`TRADING_MODE=VIRTUAL`)** — a 24/7 always-on demo using a self-generated random-walk price simulator. No real market data, no token dependency, cannot go stale. Good for showing the system running continuously; not a real market test.
    *   **Railway or local (`TRADING_MODE=PAPER`)** — real Upstox price data (via REST poll, §3), virtual order fills. This is the mode used for genuine strategy testing, and the one that requires the daily manual token refresh (§3b) and is subject to the feed-health guard (§3a).
*   **Docker Containerization:** A standard `Dockerfile` runs the FastAPI server on port `8000`.
*   **Database Path Override:** The SQLite connection looks up the `DATABASE_PATH` environment variable. On Railway this points at a mounted persistent volume (`/data/trading.db`) so trade history and daily stats survive redeploys; locally it defaults to `trading.db` in the project root.
*   **WAL Mode:** SQLite uses Write-Ahead Logging and shared connection cursors to support concurrent reads (dashboard polling) and writes (position updates from the execution loop) without locking or deadlocks.

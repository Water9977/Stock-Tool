# Full System Audit — Stock Agent Trading System

**Date:** 2026-07-03
**Auditor:** Claude (Fable 5) — read-only audit, no code changed
**Scope:** Every file in `src/`, dashboard, deployment configs, docs, installed dependencies, plus external verification against Upstox docs, Gemini API docs, SEBI circulars, and live yfinance behavior.
**Current runtime config (from `.env`):** `TRADING_MODE=PAPER`, `STARTING_CAPITAL=300000`, `MAX_DAILY_LOSS=200000`, `MAX_CAPITAL_PER_TRADE=150000`, `CANDLE_PERIOD_SECONDS=300`, `UPSTOX_ACCESS_TOKEN` set manually.

---

## Table of Contents

1. [How the System Actually Works (Process Map)](#1-process-map)
2. [Externally Verified Facts](#2-externally-verified-facts)
3. [Findings — Critical](#3-critical-findings)
4. [Findings — High](#4-high-findings)
5. [Findings — Medium](#5-medium-findings)
6. [Findings — Low / Cosmetic](#6-low-findings)
7. [Deep Dive: The Brain Module (Cold Path)](#7-brain-deep-dive)
8. [Proposed New Brain Architecture](#8-new-brain-architecture)
9. [Brain as an Agent Skill / Plugin — Evaluation](#9-brain-as-skill)
10. [gstack Integration Plan](#10-gstack-integration)
11. [Prioritized Remediation Roadmap](#11-roadmap)
12. [Open Questions for the Owner](#12-open-questions)

---

<a name="1-process-map"></a>
## 1. How the System Actually Works (Process Map)

### Startup sequence (`src/main.py` → `startup_event`)

1. `init_db()` — creates SQLite tables (`orders`, `daily_stats`), runs column migrations, enables WAL.
2. Optional `RESET_ON_START` wipe.
3. `start_day_stats_async(Config.STARTING_CAPITAL)` — inserts/updates **today's** row in `daily_stats`. **This is the only place a daily row is ever created** (see finding C4).
4. `risk_engine.load_from_db()` — restores the consecutive-loss counter.
5. `authenticate_upstox()` — VIRTUAL returns a mock token; PAPER/LIVE uses `UPSTOX_ACCESS_TOKEN` env var if set, otherwise an **interactive terminal OAuth prompt** (see M16).
6. `MarketFeed()` singleton starts (VIRTUAL simulator or live feed).
7. `sync_open_positions()` — reloads open positions from SQLite into the in-memory `_open_positions` dict.
8. Four background tasks are spawned with `asyncio.create_task(...)` and **their references are thrown away** (see H6): `monitor_open_positions`, `automated_execution_loop`, `shutdown_scheduler`, and (inside the execution loop) `technicals.run`, `brain.poll_rss_feeds`, `brain.run`.

### Hot Path (execution core)

- **Feed** (`src/market_feed.py`): In PAPER/LIVE, tries the Upstox V3 WebSocket (protobuf). If no real tick arrives within 30 s (free-plan behavior), cancels WS and falls back to a 1-second REST poll of `/v2/market-quote/ltp` for all 40 instruments. Every tick updates `price_cache` and `last_tick_time`, and aggregates into wall-clock 5-minute candles. Only the **close of a completed candle** is broadcast to the Warm Path queue.
- **Feed health**: `is_feed_healthy()` returns False if no tick for 10 s, and enforces an 8 s recovery grace period. Both the execution loop and the position monitor stand down on an unhealthy feed. VIRTUAL is always "healthy".
- **Position monitor** (`orders.monitor_open_positions`): every 100 ms, scans `_open_positions`, compares LTP against per-position target/stop-loss, simulates exit (VIRTUAL/PAPER) or fires a real market order (LIVE), then batch-writes closures to SQLite in a worker thread.

### Warm Path (indicators)

- `TechnicalEngine.run` consumes candle-close ticks from the queue, feeds each price into talipp `RSI(14)` and `MACD(12,26,9)` per symbol, and publishes a per-symbol signal:
  - BUY if `RSI < 40` or (MACD bullish and `RSI < 50`)
  - SELL if `RSI > 60` or (MACD bearish and `RSI > 50`)
  - else NEUTRAL.
- Signals require at least 26 candle closes (~130 minutes of live market data after each restart — see H4).

### Cold Path (brain)

- `BrainEngine.run` loops every ~5 minutes during market hours:
  1. Fetch yfinance news headlines for all 40 symbols in parallel (**currently returns empty strings for every headline** — see C3).
  2. `FraudAgent` keyword scan over those (empty) headlines.
  3. Collect trailing 10 RSI/MACD values per symbol from the Warm Path.
  4. Collect available balance + open positions.
  5. One `CommitteeAgent` Gemini call (`gemini-3.1-flash-lite`, structured JSON) returns a per-ticker gate: `ALLOW_BUY / ALLOW_SELL / ALLOW_ALL / VETO / FLATTEN` plus conviction, sentiment, reasoning.
  6. Gates dict is updated in place; **conviction and sentiment are discarded** (M8).
- `poll_rss_feeds` fetches 10 Livemint macro headlines every 5 minutes for the prompt and the dashboard.

### Decision loop (`automated_execution_loop`)

Every 1 second: skip if trading suspended, outside market hours, or feed unhealthy. Then for each of the 40 symbols:
`gate == FLATTEN` → emergency-close all positions for that symbol. Otherwise: if Warm-Path signal is BUY/SELL, symbol is off cooldown (120 s), and the gate permits that side → size quantity as `min(MAX_CAPITAL_PER_TRADE, available_balance) / price` and call `place_order`.

### Order lifecycle (`orders.place_order`)

Risk checks: daily-loss cap → consecutive-loss circuit breaker (3) → per-trade capital cap → available balance. VIRTUAL/PAPER: simulated fill at LTP ± 0.05 % slippage, stored as `VIRTUAL_OPEN` with target (+2 %) and stop (−1 %). LIVE: real market order to `api-hft.upstox.com/v3/order/place`, stored as `LIVE_SUBMITTED`.

### End of day

`shutdown_scheduler`: at 15:25 IST (PAPER/LIVE) or after `VIRTUAL_RUNTIME_HOURS`, suspends trading and flattens all watchlist positions. **It never re-enables trading** (H7).

### Dashboard

Static single-page app polls `/api/status`, `/prices`, `/portfolio` every 1.5 s. Also exposes `/execute` (manual order), `/admin/reset-simulation` (DB wipe), `/backtest` (yfinance replay). **None of these endpoints require any authentication** (C1).

---

<a name="2-externally-verified-facts"></a>
## 2. Externally Verified Facts (checked against live docs/behavior on 2026-07-03)

| # | Fact | Source | Impact |
|---|------|--------|--------|
| 1 | Upstox V3 place-order success response is `data.order_ids` — an **array**. There is no `data.order_id` field. | upstox.com/developer/api-documentation/v3/place-order | Code parses `res["data"]["order_id"]` → every real order looks FAILED (C2) |
| 2 | `X-Algo-Name` header is optional and, when sent, **must match an algo name configured in the Upstox app settings**, else error `UDAPI1156`. | Same page | Code hardcodes `X-Algo-Name: AntigravityStockAgent` (C6) |
| 3 | Upstox access token expires at **3:30 AM IST the next day**, regardless of when generated. | upstox.com/developer/api-documentation/get-token | Daily silent feed death without re-auth (H7) |
| 4 | Upstox order APIs: 10 req/s for regular API users (50 for registered algos), 500/min, 2000/30min. Place-order API is down 12:00 AM–5:30 AM IST. | upstox.com/developer/api-documentation/rate-limiting + place-order page | Relevant to burst behavior in the 1 s loop |
| 5 | SEBI circular (Feb 4, 2025, "Safer participation of retail investors in Algorithmic trading"): retail API algo trading requires broker permission, **static IP registration**, and defines a threshold of **10 orders per second**; enforcement timelines pushed into 2026 (brokers communicating April 2026 for static IP). | sebi.gov.in circular + broker summaries | Compliance section of architecture.md is partially wrong (C6) |
| 6 | `gemini-3.1-flash-lite` free tier: **20 RPM / 500 RPD** (Tier 1: 100 RPM / 3,000 RPD). | ai.google.dev/gemini-api/docs/rate-limits | One call per 5 min ≈ 75 calls/market-day — fine; but infinite-retry bug can burn the daily budget (H5) |
| 7 | Installed `yfinance` is **1.4.1**. `Ticker.news` items are `{'id': ..., 'content': {...}}` — the title lives at `item['content']['title']`, NOT `item['title']`. Verified with a live call for RELIANCE.NS. | Local venv inspection + live API call | NewsAgent returns empty strings for all headlines (C3) |
| 8 | Installed `talipp` 2.7.0: `len(indicator)` counts **all** outputs including `None` warm-up padding; first RSI(14) value appears at index 14; MACD signal line needs ~34 inputs. Verified locally. | Local venv test | Warm-up math in §Warm Path; existing None-guards are correct |

---

<a name="3-critical-findings"></a>
## 3. Findings — CRITICAL

### C1. Every HTTP endpoint is completely unauthenticated

**Where:** `src/main.py` — `/execute`, `/admin/reset-simulation`, `/backtest`, `/portfolio`, `/api/status`, `/prices`.
**Problem:** The app is designed for public cloud deployment (Railway/Render, Docker binds `0.0.0.0`). Anyone who discovers the URL can:
- `POST /execute` — place arbitrary orders (in LIVE mode this is **remote control of a real brokerage account**),
- `POST /admin/reset-simulation` — destroy the entire trade history and P&L records,
- `POST /backtest` — trigger unbounded yfinance downloads (free denial-of-service / cost amplification).

**Failure scenario:** A port scanner finds the Railway URL; a single unauthenticated POST wipes the DB or, in LIVE mode, buys ₹1.5 lakh of stock.

**Remediation plan:**
1. Add a shared-secret requirement (e.g., `X-API-Key` header checked against an `API_KEY` env var) enforced by a FastAPI dependency on **all** mutating endpoints (`/execute`, `/admin/reset-simulation`, `/backtest`).
2. Decide whether the dashboard read endpoints (`/api/status`, `/prices`, `/portfolio`) are public; if not, protect them too and have the frontend send the key (or serve the dashboard behind basic auth).
3. Rate-limit `/backtest` (one concurrent run) and validate `interval`/`period` against an allowlist.
4. Never expose `/admin/*` in production builds, or gate behind a second, stronger secret.

---

### C2. LIVE order responses are parsed against a field that does not exist

**Where:** `src/orders.py` → `execute_live_order` (lines ~125–127).
**Problem:** Code requires `res["data"]["order_id"]`. Upstox V3 returns `res["data"]["order_ids"]` (a list — verified against official docs). So every **successful** live order returns `{"status": "FAILED"}` to the caller.
**Failure scenario (money-losing):** In LIVE mode, `place_order` submits a real market order → broker fills it → code thinks it failed → nothing is written to the DB, nothing enters `_open_positions` → **a real position exists at the broker with no stop-loss monitoring and no record in the system**. Worse, exits in `monitor_open_positions` use the same function: a triggered stop-loss would fire the closing order, get "FAILED", `continue`, and retry every 100 ms — machine-gunning duplicate market orders at the broker until the rate limiter or margin blocks them.

**Remediation plan:**
1. Parse `data.order_ids[0]` (and store the full list if slicing is ever enabled).
2. Add an explicit response-shape test using the documented sample payload.
3. After placing, confirm via the Order Details/History API before treating the order as final; only then update `_open_positions` / DB.
4. Add idempotency: tag orders (the `tag` field, ≤40 chars) with the internal order UUID so duplicates are detectable.

---

### C3. News pipeline is silently dead — yfinance schema changed

**Where:** `src/brain.py` → `NewsAgent.fetch_yf_news` (`item.get('title', '')`).
**Problem:** Installed yfinance 1.4.1 returns news items as `{'id':…, 'content': {'title': …, 'summary': …, 'pubDate': …}}`. `item.get('title','')` therefore yields `''` for every article. Verified live: RELIANCE.NS returned 10 articles, all with `item.get('title') → None`.
**Consequences (all confirmed by `server_out.log`):**
- Committee receives 5 blank lines per symbol as "NEWS HEADLINES" → logs show every symbol reasoned as "No specific news identified."
- **FraudAgent can never fire** — the entire fraud/governance safety layer is inert.
- The Cold Path is effectively burning Gemini quota to analyze nothing.

**Remediation plan:**
1. Read `item['content']['title']` (and optionally `summary`, `pubDate`, `provider.displayName`), with a defensive fallback to the old flat shape.
2. Filter empty strings out; if all headlines are empty, pass an explicit `"No news available"` marker instead of blank lines.
3. Add a startup self-check: fetch news for one symbol; if all titles are empty, log a loud warning (schema drift detector).
4. Pin `yfinance` in `requirements.txt` (it currently has **no version pins at all** — see M15) and add a small contract test for the news shape.

---

### C4. Midnight rollover corrupts accounting and disables the account (24/7 deployments)

**Where:** `src/db.py` — every daily function keys on `date.today().isoformat()`, but the `daily_stats` row is only created in `startup_event`.
**Problem:** The app is deployed to run continuously. After the calendar date changes:
- `update_realized_pnl` runs `UPDATE … WHERE date = ?` on a row that doesn't exist → **silent no-op → realized P&L of every trade closed after midnight is permanently lost**.
- `get_realized_pnl` returns 0.0 → daily-loss circuit breaker sees no losses.
- `get_starting_capital` falls back to the hardcoded default **2000.0** → `get_available_balance = max(0, 2000 + 0 − locked)` → with any open positions this is 0 → all new trades rejected with "insufficient balance."
- `get/set_consecutive_losses` silently reset → circuit breaker state lost.

Additionally, `date.today()` uses the **server's local timezone**. On a UTC cloud host, "today" flips at 05:30 IST (pre-market — mostly harmless), but the semantics are accidental, not designed.

**Remediation plan:**
1. Create a small daily-rollover routine: at first activity of a new (IST) date — or via a scheduled task at 08:00 IST — insert the new `daily_stats` row (carrying capital forward as `previous starting_capital + previous realized_pnl`, or reloading from config, per the owner's intent — see Open Question 3).
2. Make `update_realized_pnl` use an UPSERT (`INSERT … ON CONFLICT(date) DO UPDATE`) so a missing row can never silently swallow P&L.
3. Standardize all "today" computations on IST (`datetime.now(pytz.timezone("Asia/Kolkata")).date()`).
4. Remove the misleading `2000.0` fallback in `get_starting_capital`; fail loudly or fall back to `Config.STARTING_CAPITAL`.

---

### C5. LIVE stop-losses exist only in app memory — no broker-side protection

**Where:** `src/orders.py` — `place_order` (LIVE branch) and `monitor_open_positions`.
**Problem:** In LIVE mode the system places only the entry market order. Target and stop-loss live purely in the Python process. If the process crashes, the host restarts, the token expires, or the feed goes stale (which deliberately pauses the monitor), **real positions sit at the broker with zero protective orders**. Also, when the monitor does close a position, it computes P&L from the *local cached price* and assumes an instant full fill — no fill confirmation, no partial-fill handling, no real execution price.

**Remediation plan:**
1. On LIVE entry, immediately place a broker-side SL order (`SL-M` with `trigger_price`) alongside the entry, and manage the target either as a limit order or app-side. (Upstox V3 supports `SL`/`SL-M` order types — verified in docs.)
2. Reconcile on startup and periodically: pull broker positions + order book (`/v2/portfolio/short-term-positions`, order details API) and diff against `_open_positions`; alert on any mismatch.
3. Record actual fill prices from the order/trade book instead of the local LTP estimate.
4. If the app must exit positions itself, confirm order status transitions (submitted → complete) before deleting from `_open_positions` — currently deletion happens on "SUBMITTED".

---

### C6.
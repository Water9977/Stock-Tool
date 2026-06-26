# Master AI Brain Architecture: Three-Path Execution Model

This document serves as the absolute "Truth Engine" for the Stock Tool system design. It outlines the decoupled, asynchronous architecture separating hot-path execution, warm-path technical calculations, and cold-path AI sentiment analysis using Gemini 3.1 Flash-Lite.

---

## 1. The Core Paradigm: Decoupled Loops

Since LLMs are constrained by inference speeds (1–3 seconds minimum), they cannot live in the execution loops of high-frequency or retail intraday trading (which require sub-second executions). The engine is strictly divided into three asynchronous paths with bounded latency budgets.

```
+--------------------------------------------------------------------------------+
|                                    HOT PATH                                    |
|   [Upstox V3 WS Feed] ---> [In-Memory Price Cache] ---> [SL / TP Exit Engine]  |
|   Latency: <10ms | Database: None | I/O: None | LLM: None                       |
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
|   Latency: 1-15s | Outputs: Conviction Score, Action Gate (ALLOW/VETO)         |
+--------------------------------------------------------------------------------+
```

---

## 2. Path Detail Specifications

### 🟥 HOT PATH (Execution Core & Exits)
*   **Latency Budget:** < 10ms.
*   **Role:** Real-time quote streaming and emergency position exits.
*   **Safety Rule:** This loop must never run blockages. No print logs to disk, no file reads, no external API calls, and no LLMs.
*   **Exits:** It continuously cross-checks active database positions (loaded in-memory on startup) against the WebSocket price cache. If a stop-loss (SL) or target-profit (TP) threshold is crossed, it fires an exit order instantly.

### 🟨 WARM PATH (Technical Calculations)
*   **Latency Budget:** 10–100ms.
*   **Role:** Processes ticks to update technical indicators and generate signals.
*   **Engine:** Uses the `talipp` library for incremental computations, updating indicators like RSI (Relative Strength Index) and MACD (Moving Average Convergence Divergence) on each incoming price tick.
*   **Shedding:** If tick queues fill during high volatility, a shedding mechanism drops intermediate ticks to keep indicators current.

### 🟦 COLD PATH (The AI Sentiment Gate & Safety Guardrails)
*   **Latency Budget:** 1–15 seconds.
*   **Role:** An asynchronous risk-veto and macro-guardrail gate. 
*   **Strategy Clarity (Hybrid Guardrail):** The AI does *not* directly choose or execute entries. Instead, the core technical indicators (RSI/MACD in the Warm Path) generate baseline signals, and the AI acts as a slow-moving filter. It scans news and macro feeds every **5 minutes (300 seconds)** to approve, block (`VETO`), or force-close (`FLATTEN`) trades based on market sentiment.
*   **Gemini Quota Optimization:** To prevent daily quota exhaustion (500 free requests/day), the `BrainEngine` maintains an in-memory headlines cache. Before invoking Gemini, it compares the newly fetched news headlines with the cache. If unchanged, the API call is skipped and the previous gate is reused.
*   **Data Aggregators:**
    *   **`yfinance`:** Free ticker-based news (e.g. `RELIANCE.NS`).
    *   **Livemint Markets RSS:** Free macro market sentiment news fetched asynchronously using `aiohttp` and `feedparser`.
*   **AI Engine:** Queries `gemini-3.1-flash-lite`. Returns a structured JSON output enforcing a strict schema (`ALLOW_BUY`, `ALLOW_SELL`, `ALLOW_ALL`, `VETO`, or `FLATTEN`).
*   **FLATTEN Safety Switch:** If Gemini detects a major negative catalyst (e.g., fraudulent earnings, regulatory bans, macro crashes), it returns a `FLATTEN` gate. In the next evaluator tick, `main.py` immediately intercepts this status, bypasses regular entry loops, and calls `flatten_positions_for_symbol` to place emergency market exit orders for any active holdings on that ticker.

---

## 3. Regulatory Compliance & Network Architecture

### SEBI Algorithmic Trading Guidelines (February 4, 2025 Updates)
Under SEBI guidelines, retail algorithmic trading via APIs must incorporate strict risk-management and system safeguards to ensure market orderliness:
1. **Pre-Trade Risk Management:** Our engine implements automated capital limits (`MAX_CAPITAL_PER_TRADE`) and maximum loss limits (`MAX_DAILY_LOSS`) enforced at the database level.
2. **Order Placement Cooldowns:** A mandatory 5-minute (300 seconds) cooldown is enforced per symbol to prevent runaway execution loops or rapid order spamming.
3. **Execution Compliance:** Automatic target profits (2%) and stop losses (1%) are monitored continuously in-memory to prevent open-ended exposure.
4. **Timezone-Aware Market Hours Guard:** The system restricts active order placement to official market hours (09:15 to 15:20 IST) on weekdays. In VIRTUAL mode, this guard is bypassed for 24/7 testing.
5. **Close-Market Auto-Flatten (15:25 IST):** At 15:25 IST, a background task automatically suspends trading, cancels open virtual orders, and flattens all positions to prevent overnight carrying risk (valid for PAPER/LIVE modes).

### Static IP Whitelisting Constraints
* **Mandatory Broker Whitelisting:** Upstox mandates that all order placement, modification, and cancellation requests originate from registered static IP addresses. Requests from dynamic or unregistered IPs are immediately blocked.
* **Cloud NAT Gateway Routing:** When deploying in container environments (e.g. Railway or Render) that use rotating outbound IPs, the application must route all traffic destined for `api-hft.upstox.com` through a **Static IP Proxy** (such as QuotaGuard Static or an AWS NAT Gateway). This static IP must be registered in the Upstox Developer Console.

---

## 4. Phase 2 Additions (Live Feed + Backtesting)

### Module Status

| Module | Status | Description |
|--------|--------|-------------|
| `src/market_feed.py` | ✅ Phase 2 | **LIVE:** Upstox V3 WebSocket + Protobuf decode via `upstox_client.feeder.proto.MarketDataFeedV3_pb2`. Exponential back-off reconnection. **VIRTUAL:** Random-walk Gaussian simulator. |
| `src/backtest.py` | ✅ Phase 2 | Downloads real NSE OHLCV data from Yahoo Finance. Feeds tick-by-tick through TechnicalEngine. Reports: Win Rate, P&L, Max Drawdown, trade-by-trade log. Includes realistic slippage (0.05%) and government tax charges. |
| `src/technicals.py` | ✅ Stable | RSI(14) + MACD(12,26,9) via `talipp`. Signal thresholds: RSI < 35 = BUY, RSI > 65 = SELL. |
| `src/brain.py` | ✅ Stable | Gemini sentiment gating + Livemint RSS aggregation. Uses headlines hashing cache. |
| `src/orders.py` | ✅ Stable | VIRTUAL: SQLite (WAL mode, single-transaction cursors). LIVE: Upstox `/v3/order/place` HFT REST API (with `market_protection: -1`). |
| `src/auth.py` | ✅ Stable | Programmatic TOTP login for daily token refresh. |

### Live WebSocket Authorization Flow

```
1. POST /auth/callback  →  access_token stored
2. feed.set_access_token(token)
3. GET  /v3/feed/market-data-feed/authorize  →  authorizedRedirectUri (wss://)
4. CONNECT wss://...   (binary protobuf stream)
5. SEND JSON: { method: "sub", data: { instrumentKeys: [...], mode: "full" } }
6. RECV binary frames  →  FeedResponse.ParseFromString()
7. FeedResponse.feeds[key].fullFeed.marketFF.ltpc.ltp  →  price_cache update
```

### Backtesting CLI

```bash
python src/backtest.py --symbol RELIANCE.NS --interval 1h --period 1mo
python src/backtest.py --symbol TCS.NS      --interval 5m  --period 5d
```

Or trigger from the dashboard: **Backtest Lab** tab → select symbol/interval/period → **▶ Run Backtest**.

---

## 5. Financial Safety Configurations

*   **Max Daily Loss:** Realized P&L is checked at the database level. If losses exceed `MAX_DAILY_LOSS` (e.g. Rs. 2,000), the app suspends all operations.
*   **Max Capital Per Trade:** The value of a single trade (LTP * Quantity) cannot exceed `MAX_CAPITAL_PER_TRADE`.
*   **Starting Capital:** The system tracks an initial `STARTING_CAPITAL` (e.g. Rs. 50,000). To ensure there is sufficient available balance to execute trades, `STARTING_CAPITAL` must be **at least 2x the price of the most expensive stock in the watchlist** (e.g., TCS ~Rs. 3300).
*   **TOTP Automation:** Headlessly handles mornings via `pyotp` (free, secure authentication).
*   **5-Minute Cooldown:** Prevents repeated orders on the same symbol within 300 seconds.

**Railway Environment Variables Requirements:**
When deploying to Railway (or any cloud environment), configure the following environment variables (these define the values described above):
- `STARTING_CAPITAL=50000`
- `MAX_CAPITAL_PER_TRADE=10000`
- `MAX_DAILY_LOSS=2000`

---

## 6. Deployment & Database Persistence

The system is configured for cloud deployment to support 24/7 execution without requiring a personal computer to stay active.

*   **Docker Containerization:** A standard [Dockerfile](file:///e:/Antigravity/Workspaces/Stcok%20Agent/Dockerfile) is defined to run the FastAPI server on port `8000`.
*   **Database Path Override:** The SQLite connection looks up the `DATABASE_PATH` environment variable. By default, it points to local `trading.db`.
*   **Persistent Storage:** When deploying to platforms like Railway or Render, a persistent volume should be mounted (e.g., at `/data/trading.db`) to prevent losing simulation history or daily statistics.
*   **WAL mode Enabled:** SQLite utilizes Write-Ahead Logging (WAL) mode and shared connection cursors to support concurrent reads (API status dashboard calls) and writes (position updates from execution loop) without locking or deadlocks.


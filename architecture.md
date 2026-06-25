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
|   Latency: 10-100ms | Calculations: RSI, MACD, EMAs                            |
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
*   **Engine:** Uses the `talipp` library for incremental computations, updating indicators like RSI (Relative Strength Index) and MACD (Moving Average Convergence Divergence) on each candle close.
*   **Shedding:** If tick queues fill during high volatility, a shedding mechanism drops intermediate ticks to keep indicators current.

### 🟦 COLD PATH (The AI Sentiment Gate)
*   **Latency Budget:** 1–15 seconds.
*   **Role:** Gating mechanism that approves or vetoes signals.
*   **Data Aggregators:**
    *   **`yfinance`:** Free ticker-based news (e.g. `RELIANCE.NS`).
    *   **Livemint Markets RSS:** Free macro market sentiment news fetched asynchronously using `aiohttp` and `feedparser`.
*   **AI Engine:** Queries `gemini-3.1-flash-lite` via the Google AI Studio free tier. Returns a structured JSON output enforcing a strict schema (`ALLOW_BUY`, `ALLOW_SELL`, `ALLOW_ALL`, `VETO`, or `FLATTEN`).
*   **Safety Switch:** If Gemini returns `ALLOW_ALL`, technical indicators are trusted entirely. If it returns `FLATTEN`, it triggers an immediate exit of the position.


---

## 4. Phase 2 Additions (Live Feed + Backtesting)

### Module Status

| Module | Status | Description |
|--------|--------|-------------|
| `src/market_feed.py` | ✅ Phase 2 | **LIVE:** Upstox V3 WebSocket + Protobuf decode via `upstox_client.feeder.proto.MarketDataFeedV3_pb2`. Exponential back-off reconnection. **VIRTUAL:** Random-walk Gaussian simulator. |
| `src/backtest.py` | ✅ Phase 2 | Downloads real NSE OHLCV data from Yahoo Finance. Feeds tick-by-tick through TechnicalEngine. Reports: Win Rate, P&L, Max Drawdown, trade-by-trade log. |
| `src/technicals.py` | ✅ Stable | RSI(14) + MACD(12,26,9) via `talipp`. Signal thresholds: RSI < 35 = BUY, RSI > 65 = SELL. |
| `src/brain.py` | ✅ Stable | Gemini sentiment gating + Livemint RSS aggregation. |
| `src/orders.py` | ✅ Stable | VIRTUAL: SQLite. LIVE: Upstox `/order/place` REST API. |
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
*   **TOTP Automation:** Headlessly handles mornings via `pyotp` (free, secure authentication).
*   **5-Minute Cooldown:** Prevents repeated orders on the same symbol within 300 seconds.

---

## 6. Deployment & Database Persistence

The system is configured for cloud deployment to support 24/7 execution without requiring a personal computer to stay active.

*   **Docker Containerization:** A standard [Dockerfile](file:///e:/Antigravity/Workspaces/Stcok%20Agent/Dockerfile) is defined to run the FastAPI server on port `8000`.
*   **Database Path Override:** The SQLite connection looks up the `DATABASE_PATH` environment variable. By default, it points to local `trading.db`.
*   **Persistent Storage:** When deploying to platforms like Railway or Render, a persistent volume should be mounted (e.g., at `/data/trading.db`) to prevent losing simulation history or daily statistics during container updates or restarts.


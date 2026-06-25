# Stock Tool - System Architecture

Welcome to the **Stock Tool** system architecture document. This file acts as the primary "Truth Engine" and documentation hub for our autonomous trading system. It is kept up-to-date with every architectural update.

---

## 1. System Goals & Context
* **Objective:** An automated, self-executing trading tool for Indian retail stock markets.
* **Target Brokers:** Upstox (primary) and Zerodha (secondary capability).
* **Speed:** Low execution latency (45ms to 120ms total loop time) achieved via regional cloud deployment and WebSocket streaming.
* **Cost:** 100% free infrastructure and API usage.
* **Security:** 100% secure storage of 2FA secrets, local order databases, and isolated execution scopes.

---

## 2. Technology Stack & Cost Profile

| Component | Technology Choice | Cost | Justification |
| :--- | :--- | :--- | :--- |
| **Language** | Python 3.11+ | $0 | Standard for quantitative analysis, data processing, and broker SDKs. |
| **Execution Broker** | Upstox API v2 | $0 | Upstox offers 100% free API subscriptions (including live data feeds) for retail users. |
| **Web Server** | FastAPI & Uvicorn | $0 | Async-first, high performance, low footprint API to trigger trades. |
| **Database** | SQLite3 | $0 | Zero-setup, file-based SQL database, perfect for local position and order logging. |
| **Auth Automation** | `pyotp` (Python TOTP library) | $0 | Eliminates the need for Selenium or headless browsers to solve 2FA login. |
| **Hosting** | AWS Free Tier (EC2 ap-south-1) | $0 (Trial) | Located in Mumbai to keep ping latency to Upstox/Zerodha engines under 10ms. |

---

## 3. High-Level Application Flow

```
                      +-----------------------------+
                      |         Cron Scheduler      | (Daily at 8:45 AM)
                      +--------------+--------------+
                                     |
                                     ▼
                      +-----------------------------+
                      |    Automated TOTP Login     | (pyotp + requests)
                      +--------------+--------------+
                                     |
                                     ▼
                      +-----------------------------+
                      |    Store access_token       | (Saved to local .session.json)
                      +--------------+--------------+
                                     |
                                     ▼
                      +-----------------------------+
                      |     Start WebSocket Feed    | (Keep-alive price streaming)
                      +--------------+--------------+
                                     |
                                     ▼
+---------------------+       +------+--------------+
|     The "Brain"     |       |   In-Memory Price   |
|  (Technical/News    |       |        Cache        |
|  Research Engine)   |       +------+--------------+
+----------+----------+              |
           |                         |
           | (HTTP POST /execute)    | (Matches criteria)
           ▼                         ▼
+---------------------+       +---------------------+
|  FastAPI Endpoints  |------>|   Safety Controls   | (Max loss / Size checks)
+---------------------+       +------+--------------+
                                     |
                                     ▼
                              +------+--------------+
                              | Upstox Order API    | (Execute Trade)
                              +------+--------------+
                                     |
                                     ▼
                              +------+--------------+
                              |  Local SQLite DB    | (Log Execution Status)
                              +---------------------+
```

### Steps:
1. **Startup (8:45 AM):** The background service boots up. It programmatically gets the latest TOTP token via `pyotp` and performs an HTTP session login flow to grab the daily `access_token` from Upstox.
2. **Streaming (9:00 AM):** The WebSocket feed starts. It streams ticks and updates a fast, in-memory cache of live quotes.
3. **Execution Ready (9:15 AM):** FastAPI server is live and listening.
4. **Triggering Trades:** The trade signal component (the Brain) posts a request to `/execute`. The engine runs safety limits (checks if daily loss limits are exceeded), formats the payload, and posts directly to Upstox via a persistent connection pool. It then saves logs to `trading.db`.

---

## 4. Security Framework
* **No hardcoded secrets:** Credentials (Client ID, Secret, Mobile, Pin, TOTP Secret) are loaded exclusively via environment variables using a `.env` file.
* **Isolated Environment:** The application runs inside a virtual environment (`venv`).
* **IP Whitelisting:** Orders will only be accepted from the whitelisted static Elastic IP of our hosting server.
* **Risk Engine (Safety Switch):** Max daily loss ceiling is checked before *any* order placement. If hit, the engine suspends trading and sends a notification.

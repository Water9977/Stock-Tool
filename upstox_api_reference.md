# Upstox API Reference — PAPER Mode Setup Guide
*Compiled from live docs on 2026-06-26. Relevant to transitioning from VIRTUAL → PAPER mode.*

---

## 1. Authentication Flow (OAuth 2.0)

Upstox uses **OAuth 2.0 Authorization Code** flow. The token expires **daily** — must regenerate every day.

### Three Ways to Get a Token

| Method | Best For | Our Use Case |
|---|---|---|
| Interactive OAuth (browser redirect) | End-user apps | ❌ Requires manual browser login |
| **Semi-automated** | Scheduled/automated apps | ✅ **This is what we should use** |
| Manual (copy-paste from dashboard) | One-off testing | ✅ OK for initial testing |

### Semi-Automated Flow (Recommended for PAPER mode)

1. App triggers an **Access Token Request** at a fixed time (e.g. 08:50 AM IST via cron)
2. Upstox sends a **push notification** to your phone
3. You tap **Approve** on your phone
4. Token is **pushed to your notifier URL** — Railway receives it automatically
5. System stores it and uses it all day

> **Key insight**: No TOTP automation needed. You just tap "approve" on your phone once every morning before market open.

### Full OAuth Steps (for reference)

```
Step 1 → GET https://api.upstox.com/v2/login/authorization/dialog
         ?response_type=code&client_id=<API_KEY>&redirect_uri=<YOUR_URI>

Step 2 → Upstox redirects to: <redirect_uri>?code=mk404x

Step 3 → POST https://api.upstox.com/v2/login/authorization/token
         Body: code=<code>&client_id=<KEY>&client_secret=<SECRET>
               &redirect_uri=<URI>&grant_type=authorization_code

Response → { "access_token": "eyJ...", "expires_in": 86400 }
```

### Manual Token (Easiest for Today)
Go to → https://account.upstox.com/developer/apps → Click your app → Click **Generate**
Copy the token. Valid for 24 hours. Set as Railway env var: `UPSTOX_ACCESS_TOKEN`.

---

## 2. V3 Order Placement API (CRITICAL — current code uses V2)

> [!WARNING]
> Our `src/orders.py` currently calls `https://api.upstox.com/v2/order/place`. 
> **This must be updated to V3** for PAPER/LIVE mode.

### Correct V3 Endpoint
```
POST https://api-hft.upstox.com/v3/order/place
```

Note: `api-hft.upstox.com` — NOT `api.upstox.com`. Different hostname.

### Required Request Body
```json
{
  "quantity": 1,
  "product": "I",
  "validity": "DAY",
  "price": 0,
  "instrument_token": "NSE_EQ|INE002A01018",
  "order_type": "MARKET",
  "transaction_type": "BUY",
  "disclosed_quantity": 0,
  "trigger_price": 0,
  "is_amo": false,
  "slice": false,
  "market_protection": -1
}
```

### Key Fields Explained

| Field | Value | Notes |
|---|---|---|
| `product` | `"I"` | Intraday. `"D"` = Delivery/CNC |
| `order_type` | `"MARKET"` | But read `market_protection` note below |
| `market_protection` | `-1` | Auto. `0` = No protection (REJECTED by exchange). `1-25` = % protection |
| `is_amo` | `false` | System auto-detects; ignore this flag |
| `slice` | `false` | Only needed for large F&O orders |

> [!CAUTION]
> `market_protection: 0` = order WILL be rejected by the exchange.
> Always set to `-1` (auto) for market orders. Error code `UDAPI1158` if market orders blocked.

### Sandbox Order Endpoint (for testing)
```
POST https://api-sandbox.upstox.com/v3/order/place
```
Uses a sandbox token (valid 30 days). Does NOT touch real money or real market.

### Response
```json
{
  "status": "success",
  "data": { "order_ids": ["1644490272000"] },
  "metadata": { "latency": 30 }
}
```

### Order Availability Hours
- Place Order API is **DOWN from 12:00 AM to 5:30 AM IST** daily for maintenance
- Our market-hours guard (09:15–15:20 IST) already keeps us safe

---

## 3. Rate Limits (Per User)

### Order Placement APIs (Place/Modify/Cancel)

| Window | Regular Algo | SEBI-Registered Algo |
|---|---|---|
| Per second | **10 requests** | 50 requests |
| Per minute | 500 requests | 500 requests |
| Per 30 min | 2000 requests | 2000 requests |

### Other APIs (Holdings, Positions, Funds, Historical Data)

| Window | Limit |
|---|---|
| Per second | 50 requests |
| Per minute | 500 requests |

**Our current system**: 1s execution loop on 5 stocks = max ~5 order attempts/sec. Well within limits.

---

## 4. Sandbox — What It Is

**The Sandbox is Upstox's own built-in testing environment.** Think of it like a video game version of trading — it accepts real API calls but nothing hits the real exchange.

### How It Differs From Our Current VIRTUAL Mode

| | Our VIRTUAL Mode | Upstox Sandbox |
|---|---|---|
| Price data | Random walk (fake) | Real market prices (via API) |
| Order execution | Local Python code | Upstox backend processes it |
| Real account needed | No | Yes (your real Upstox account) |
| Token expires | Never (mock) | Every 30 days |
| Cost | Free | Free |
| Market hours restriction | None (24/7) | None (24/7 for sandbox) |

### When to Use Sandbox

**Use Upstox Sandbox when:**
- You want to test order placement code with realistic API responses
- You want to confirm the V3 API payload is correct before going live
- You want to test error handling (rejection, insufficient funds, etc.)

**Supported Sandbox APIs:**
- Place Order V3 ✅
- Modify Order V3 ✅
- Cancel Order V3 ✅
- Place Multi Order ✅

**NOT supported in sandbox** (yet):
- WebSocket market data feed
- Portfolio positions read
- Funds/margins

### How to Create a Sandbox Token
1. Go to → https://account.upstox.com/developer/apps#sandbox
2. Click **New Sandbox App**
3. Fill form → Click Continue
4. Click **Generate** → Copy token (valid 30 days)
5. Set as env var: `UPSTOX_SANDBOX_TOKEN`
6. Point orders to: `https://api-sandbox.upstox.com/v3/order/place`

---

## 5. Static IP Requirement for Live Orders

> [!CAUTION]
> **From Upstox docs**: Error `UDAPI1154` = "Access to this API is blocked due to static IP restrictions."
> Order placement from API requires a **whitelisted static IP** registered in your app settings.
> Railway's IP is **not static** — it changes on each deploy.

### Solutions for PAPER/LIVE Mode

| Option | Cost | Complexity |
|---|---|---|
| Railway static IP addon | ~$5/month | Low — one click in Railway settings |
| VPN with static IP (Mullvad/ProtonVPN) | ~$5/month | Medium |
| Dedicated VPS (DigitalOcean) | ~$6/month | High — full server management |

**Recommended**: Railway Static IP addon. Then whitelist that IP in Upstox Developer Console under app settings.

---

## 6. What Needs to Change in Code for PAPER Mode

### `src/orders.py` — V2 → V3 Migration

```python
# CURRENT (BROKEN for live):
url = "https://api.upstox.com/v2/order/place"
payload = { "quantity": qty, "product": "D", ... }

# CORRECT V3:
url = "https://api-hft.upstox.com/v3/order/place"
payload = {
    "quantity": qty,
    "product": "I",           # Intraday
    "validity": "DAY",
    "price": 0,
    "instrument_token": symbol,
    "order_type": "MARKET",
    "transaction_type": action,
    "disclosed_quantity": 0,
    "trigger_price": 0,
    "is_amo": False,
    "slice": False,
    "market_protection": -1   # CRITICAL: -1 = auto protection
}
```

### `src/main.py` — Market Hours Guard

```python
import pytz
from datetime import datetime, time

def is_market_hours() -> bool:
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist).time()
    return time(9, 15) <= now <= time(15, 20)

# In execution loop:
if not is_market_hours():
    await asyncio.sleep(60)
    continue
```

### `src/auth.py` — Daily Token

For PAPER mode: Set `UPSTOX_ACCESS_TOKEN` env var manually every morning after approving the semi-automated request from your phone.

---

## 7. Token Strategy for Tomorrow's Session

**Simplest path to PAPER mode:**

```
Morning Routine (before 9:15 AM):
1. Open https://account.upstox.com/developer/apps
2. Click your app → Click "Generate"
3. Copy the access token
4. Go to Railway dashboard → trading-agent service → Variables
5. Set: UPSTOX_ACCESS_TOKEN = <paste token>
6. Set: TRADING_MODE = PAPER
7. Railway auto-redeploys
8. System now uses real Upstox WebSocket + real prices
```

---

## 8. Summary of API URLs

| Action | URL |
|---|---|
| Auth dialog | `https://api.upstox.com/v2/login/authorization/dialog` |
| Token exchange | `https://api.upstox.com/v2/login/authorization/token` |
| **Place order V3 (live)** | `https://api-hft.upstox.com/v3/order/place` |
| **Place order V3 (sandbox)** | `https://api-sandbox.upstox.com/v3/order/place` |
| WebSocket V3 feed | `wss://api.upstox.com/v3/feed/market-data-feed` |
| Positions | `https://api.upstox.com/v2/portfolio/short-term-positions` |
| Holdings | `https://api.upstox.com/v2/portfolio/long-term-holdings` |
| Funds & margins | `https://api.upstox.com/v2/user/get-funds-and-margin` |
| Historical candles | `https://api.upstox.com/v2/historical-candle/{token}/{interval}/{to}/{from}` |
| Instruments CSV | `https://assets.upstox.com/market/instruments/exchange/NSE.csv.gz` |

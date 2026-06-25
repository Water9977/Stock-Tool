# Designing an Autonomous Stock Trading Agent for India (Upstox & Zerodha)

Building an autonomous agent that trades on your behalf using **Upstox** and **Zerodha (Kite Connect)** in India is fully legal, highly feasible, and technically mature. However, it requires careful adherence to security practices, SEBI guidelines, and network optimization to achieve instant execution speeds.

---

## 1. Compliance & SEBI Regulatory Landscape

Under the Securities and Exchange Board of India (SEBI) guidelines, automated trading by retail investors is legal but subject to strict system safeguards to prevent market abuse and ensure stability.

### Key Rules for Retail API Trading:
1. **Static IP Whitelisting:** SEBI mandates that order execution via client APIs must originate only from whitelisted static IP addresses. If you run your agent on a cloud VPS (e.g., AWS EC2), you must allocate an Elastic IP (static) and register it in your Upstox/Kite Connect developer console. Requests from other IPs will be blocked.
2. **Orders Per Second (OPS) Limit:** 
   - **Under 10 OPS:** Retail accounts are permitted to place up to 10 orders per second. This is classified as standard retail API usage. It does not require formal exchange approval or algorithmic audits.
   - **10+ OPS:** Any execution exceeding 10 OPS is classified as High-Frequency Trading (HFT) and requires exchange approval, algorithmic testing, and a member-hosted server.
3. **Mandatory 2FA/TOTP:** Standard password-only login is disabled. All API applications must use OAuth 2.0 with Time-Based One-Time Password (TOTP) 2-factor authentication.
4. **Market Protection Order Limits:** Pure market orders without price protection (such as SL-M with zero slippage checks) are banned or heavily restricted by exchanges to prevent runaway executions. Limit orders or Stop-Loss Limit (SL-L) orders are recommended.

---

## 2. Broker Comparison: Upstox vs. Zerodha

| Feature | Upstox API (v2) | Zerodha (Kite Connect) |
| :--- | :--- | :--- |
| **API Subscription Cost** | **Free** (Market Data + Execution) | **Free** (Personal Plan - Execution Only) / **₹500/month** (With Live Market Data) |
| **Historical Data API** | Included (Free) | Paid (Part of the ₹500/month plan) |
| **SDK Support** | Python, Node.js, PHP, Java | Python, Node.js, Java, Go, Rust |
| **Authentication Flow** | OAuth 2.0 + TOTP | OAuth 2.0 + TOTP |
| **Execution Speeds** | 50ms - 100ms processing | 45ms - 90ms processing |
| **Reliability** | Good, modern REST/WS structure | Excellent, battle-tested under peak loads |

> [!TIP]
> **Upstox** is the most cost-effective option for testing, as all APIs (including WebSocket and historical data) are free for retail accounts. **Zerodha** is ideal for stability under heavy market traffic but charges a subscription fee if you need real-time data feeds.

---

## 3. Programmatic Login Automation (Bypassing Manual 2FA)

Both brokers expire access tokens daily around 6:00 AM IST. To build a fully autonomous agent that runs on its own, you must automate the login process each morning using a cron job.

### Step 1: Obtain your 2FA Secret Key
1. Go to your Zerodha/Upstox account profile settings.
2. Under Password & Security, enable **External TOTP** (often requires scanning a QR code).
3. Copy the raw alphanumeric **Secret Key** (e.g., `37OP5KJL...`) instead of just scanning the QR code with Google Authenticator. Keep this secret extremely safe.

### Step 2: Implement Programmatic TOTP Generation in Python
You can use the `pyotp` library to generate the 6-digit verification code in code:

```python
import pyotp

# Store this securely in your environment variables
TOTP_SECRET = "YOUR_SHARED_TOTP_SECRET_KEY"

def generate_totp():
    totp = pyotp.TOTP(TOTP_SECRET)
    return totp.now()

print("Current 6-Digit OTP:", generate_totp())
```

### Step 3: Selenium-Free Automated Login Flow
Instead of launching a heavy headless browser like Selenium, you can authenticate using direct HTTP requests to retrieve the daily access token.

#### A. Zerodha Kite Connect Automatic Login
```python
import requests
import pyotp
from urllib.parse import parse_qs, urlparse
from kiteconnect import KiteConnect

API_KEY = "your_api_key"
API_SECRET = "your_api_secret"
USER_ID = "your_username"
PASSWORD = "your_password"
TOTP_SECRET = "your_totp_secret"

# Initialize Kite SDK
kite = KiteConnect(api_key=API_KEY)
login_url = kite.login_url()

session = requests.Session()

# 1. Submit Username and Password
r1 = session.post("https://kite.zerodha.com/api/login", data={
    "user_id": USER_ID,
    "password": PASSWORD
})
r1_data = r1.json()
request_id = r1_data["data"]["request_id"]

# 2. Submit Programmatically Generated TOTP
totp_code = pyotp.TOTP(TOTP_SECRET).now()
r2 = session.post("https://kite.zerodha.com/api/twofa", data={
    "request_id": request_id,
    "twofa_value": totp_code,
    "user_id": USER_ID
})

# 3. Request Authorization Redirect
auth_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
r3 = session.get(auth_url, allow_redirects=True)

# 4. Extract Request Token from Redirect URL
parsed_url = urlparse(r3.url)
query_params = parse_qs(parsed_url.query)
request_token = query_params["request_token"][0]

# 5. Generate and Save Access Token
session_data = kite.generate_session(request_token, secret=API_SECRET)
access_token = session_data["access_token"]
print("Access Token Generated successfully:", access_token)
```

#### B. Upstox Automatic Login
Upstox offers an automated login package called `upstox-totp` (from community resources) or you can simulate the HTTP POST flow:

```python
import requests
import pyotp
from urllib.parse import parse_qs, urlparse

API_KEY = "your_upstox_api_key"
API_SECRET = "your_upstox_api_secret"
REDIRECT_URI = "your_registered_redirect_uri"
MOBILE_NUMBER = "your_registered_mobile_number"
PIN = "your_6_digit_upstox_pin"
TOTP_SECRET = "your_upstox_totp_secret"

session = requests.Session()

# 1. Request OTP Session
r1 = session.post("https://api.upstox.com/v2/login/authorization/dialog", data={
    "client_id": API_KEY,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code"
})

# 2. Send Mobile Number
r2 = session.post("https://api.upstox.com/v2/login/send-otp", json={
    "mobile_number": MOBILE_NUMBER
})

# 3. Submit programmatically generated TOTP
totp_code = pyotp.TOTP(TOTP_SECRET).now()
r3 = session.post("https://api.upstox.com/v2/login/verify-otp", json={
    "otp": totp_code
})

# 4. Verify Pin
r4 = session.post("https://api.upstox.com/v2/login/verify-pin", json={
    "pin": PIN
})

# 5. Get Authorization Code from redirected URL
auth_url = f"https://api.upstox.com/v2/login/authorization/redirect?client_id={API_KEY}&redirect_uri={REDIRECT_URI}&response_type=code"
r5 = session.get(auth_url, allow_redirects=False)
redirect_url = r5.headers.get("Location")

parsed = urlparse(redirect_url)
auth_code = parse_qs(parsed.query)["code"][0]

# 6. Exchange Code for Access Token
token_response = requests.post("https://api.upstox.com/v2/oauth/token", data={
    "code": auth_code,
    "client_id": API_KEY,
    "client_secret": API_SECRET,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code"
})
access_token = token_response.json()["access_token"]
print("Upstox Access Token:", access_token)
```

---

## 4. Latency Optimization for "Instant" Executions

If you require your agent to sell instantly when conditions are met, you must eliminate network bottlenecks and resource delays.

### A. AWS Mumbai Cloud Hosting
- Run your agent on a cloud VPS located in the **AWS Mumbai (ap-south-1) region** (e.g., an EC2 instance or a Lightsail VPS).
- Network latency between AWS Mumbai servers and Zerodha/Upstox execution engines is **1ms - 10ms**.
- This is significantly faster and more stable than hosting on a home internet connection.

### B. WebSocket streaming vs. HTTP Polling
- Never use HTTP polling (continually calling REST endpoints) to check prices. You will hit rate limits and encounter high latencies (300ms+).
- Use **WebSockets** (Kite Ticker or Upstox WebSocket Feed) to stream real-time tick data. Ticks are pushed from the broker's servers as soon as trades occur in the exchange.
- Maintain an **in-memory cache** of the latest stock price. When the trigger condition is met, execute the order instantly without calling any external data API.

### C. Connection Pooling
- Reuse HTTP connections for placing orders. Setting up a new TCP/TLS connection for an order takes 100ms+. Keeping a session alive via `urllib3` connection pooling reduces order placement time to ~15-20ms.

---

## 5. Security Architecture (100% Secure Setup)

Since this agent handles real money, it must be hardened against unauthorized access, credential theft, and runtime crashes.

```
       ┌────────────────────────────────────────────────────────┐
       │                 AWS VPC (ap-south-1)                   │
       │                                                        │
       │  ┌───────────────────────┐    ┌─────────────────────┐  │
       │  │    Secrets Manager    │    │  EC2 Agent Instance │  │
       │  │ (API Keys, OTP Keys,  │───>│  - REST Client      │  │
       │  │  Broker Passwords)    │    │  - WebSocket Client │  │
       │  └───────────────────────┘    │  - In-Memory Engine │  │
       │                               └─────────────────────┘  │
       └───────────────────────────────┬────────────────────────┘
                                       │ (Elastic IP: Whitelisted)
                                       ▼ (SSL / TLS 1.3)
                       ┌──────────────────────────────┐
                       │  Broker APIs (Upstox/Kite)   │
                       └──────────────────────────────┘
```

### Security Best Practices:
1. **Never Hardcode Secrets:** Place all credentials, API keys, and passwords in an encrypted configuration file, a `.env` file (excluded from git via `.gitignore`), or cloud-native key vaults like AWS Secrets Manager.
2. **Access Control / SSH:** Restrict SSH access to your trading server using key-based authentication only. Turn off password access. Change default ports and restrict access to your personal IP.
3. **Daily Order/Loss Limits (Safety Switch):**
   - Implement a hard-coded maximum loss limit (e.g., "stop trading if daily loss exceeds ₹2,000").
   - Limit the maximum capital allocated per order.
   - Implement an automated system to cancel all open orders and exit positions if the connection to the WebSocket drops for more than 60 seconds.
4. **Isolated Virtual Environment:** Run the agent process inside a container (Docker) or a dedicated system user account with minimal permissions.

---

## 6. Integrating Autonomous Market Research

To make the agent buy and sell "on its own" based on market research, you can build a pipeline that feeds signals into your execution engine.

### Data Inputs:
- **Technical Indicators:** Use `pandas-ta` or `TA-Lib` to calculate indicators (RSI, MACD, EMAs, Bollinger Bands) from the WebSocket ticker data or 1-minute historical candles.
- **Sentiment Feeds:** Integrate news RSS feeds, Google News APIs, or social media scrapers to scan for stock-specific headlines, calculating a sentiment polarity score using libraries like `VADER` or transformer models (`FinBERT`).
- **Fundamental Screener:** Periodically pull quarterly earnings data (via tools like Yahoo Finance, Alpha Vantage, or broker APIs) to filter for growth stock setups (e.g., Revenue growth > 15%, low Debt-to-Equity).

### Decision Making:
1. **Scanner Component:** Periodically filters a universe of stocks (e.g., Nifty 50) for setups.
2. **Trigger Component:** WebSocket monitors the short-listed stocks in real-time.
3. **Execution Component:** Once all criteria align, places limit orders programmatically.

---

## 7. Alternative Approaches

If you prefer not to write the entire system from scratch, you can use existing middleware platforms that manage security and login, allowing you to focus on strategy:

1. **Tradetron.tech:** A popular web-based engine that supports Upstox and Zerodha. You can build strategies visually or using Python code and let Tradetron handle the API connections.
2. **Quantiply.tech:** Specialized in option automation and intraday equity strategies for Indian brokers. Extremely fast execution and handles all broker login sessions automatically.
3. **AlgoTest.in:** Highly popular for backtesting strategies and deploying them to execution via live broker bridges.

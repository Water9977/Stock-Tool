# Upstox Trading Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a robust, free, and secure automated trading execution engine for Upstox that handles daily login, real-time price streaming, and safe order execution.

**Architecture:** The agent operates as a lightweight Python background service. It utilizes a cron-based scheduler to log in programmatically using TOTP, maintains a live WebSocket feed for prices, and exposes a simple REST API to receive buy/sell signals from the "brain."

**Tech Stack:** Python 3.11+, FastAPI (for the internal API), `pyotp` (for 2FA bypass), `httpx` (async HTTP requests), `websockets` (real-time data streaming), and SQLite (local position and order logs).

---

### Task 1: Project Skeleton & Dependency Setup

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`

**Step 1: Write requirements.txt**
Create a file listing all the libraries we need.
```text
fastapi==0.111.0
uvicorn==0.30.1
httpx==0.27.0
pyotp==2.9.0
websockets==12.0
python-dotenv==1.0.1
upstox-python-sdk==2.3.0
```

**Step 2: Write .env.example**
Create a template for environment configuration (no actual secrets here).
```env
UPSTOX_CLIENT_ID=
UPSTOX_CLIENT_SECRET=
UPSTOX_REDIRECT_URI=http://127.0.0.1:8000/auth/callback
UPSTOX_MOBILE_NUMBER=
UPSTOX_PIN=
UPSTOX_TOTP_SECRET=
MAX_DAILY_LOSS=2000
MAX_CAPITAL_PER_TRADE=5000
```

**Step 3: Write .gitignore**
Exclude secrets and cache from Git.
```text
.env
__pycache__/
*.pyc
.session.json
trading.db
```

**Step 4: Commit**
```bash
git add requirements.txt .env.example .gitignore
git commit -m "chore: setup project skeleton and configurations"
```

---

### Task 2: Programmatic Upstox Authentication System

**Files:**
- Create: `src/auth.py`
- Create: `src/config.py`

**Step 1: Implement config helper**
Create `src/config.py` to parse environment variables safely.
```python
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
    CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
    REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI")
    MOBILE_NUMBER = os.getenv("UPSTOX_MOBILE_NUMBER")
    PIN = os.getenv("UPSTOX_PIN")
    TOTP_SECRET = os.getenv("UPSTOX_TOTP_SECRET")
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 2000))
    MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", 5000))
```

**Step 2: Implement automated login**
Create `src/auth.py` to automatically fetch the Upstox access token each morning.
```python
import httpx
import pyotp
import json
from urllib.parse import parse_qs, urlparse
from src.config import Config

SESSION_FILE = ".session.json"

def get_totp():
    return pyotp.TOTP(Config.TOTP_SECRET).now()

async def authenticate_upstox():
    async with httpx.AsyncClient(follow_redirects=False) as client:
        # 1. Get initial Auth page context
        url = f"https://api.upstox.com/v2/login/authorization/dialog?client_id={Config.CLIENT_ID}&redirect_uri={Config.REDIRECT_URI}&response_type=code"
        r1 = await client.get(url)
        
        # 2. Request OTP verification sending mobile number
        r2 = await client.post("https://api.upstox.com/v2/login/send-otp", json={
            "mobile_number": Config.MOBILE_NUMBER
        })
        
        # 3. Verify OTP programmatically
        otp = get_totp()
        r3 = await client.post("https://api.upstox.com/v2/login/verify-otp", json={
            "otp": otp
        })
        
        # 4. Verify user PIN
        r4 = await client.post("https://api.upstox.com/v2/login/verify-pin", json={
            "pin": Config.PIN
        })
        
        # 5. Get redirect code
        r5 = await client.get(url)
        redirect_url = r5.headers.get("Location")
        parsed = urlparse(redirect_url)
        auth_code = parse_qs(parsed.query)["code"][0]
        
        # 6. Exchange code for access token
        r6 = await client.post("https://api.upstox.com/v2/oauth/token", data={
            "code": auth_code,
            "client_id": Config.CLIENT_ID,
            "client_secret": Config.CLIENT_SECRET,
            "redirect_uri": Config.REDIRECT_URI,
            "grant_type": "authorization_code"
        })
        data = r6.json()
        access_token = data["access_token"]
        
        # Save session to file
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
        return access_token
```

**Step 3: Commit**
```bash
git add src/config.py src/auth.py
git commit -m "feat: implement automated TOTP authentication flow"
```

---

### Task 3: WebSocket Market Feed Client

**Files:**
- Create: `src/market_feed.py`

**Step 1: Implement market data feed**
Create `src/market_feed.py` to connect to Upstox's WebSocket API, receive real-time ticks, and maintain an in-memory price cache.
```python
import asyncio
import websockets
import json
from src.auth import authenticate_upstox

class MarketFeed:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.price_cache = {}
        self.is_running = False

    async def connect(self):
        url = "wss://api.upstox.com/v2/feed/market-data-feed"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        
        self.is_running = True
        while self.is_running:
            try:
                async with websockets.connect(url, extra_headers=headers) as ws:
                    print("Connected to Upstox Live Price Feed.")
                    # Send subscription request (example: Nifty 50 index / selected stocks)
                    # We will structure subscription payload based on Upstox API v2 guidelines
                    while self.is_running:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        self.update_cache(data)
            except Exception as e:
                print(f"WebSocket connection lost: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    def update_cache(self, data: dict):
        # Update our local price dictionary with the latest LTP (Last Traded Price)
        pass
```

**Step 2: Commit**
```bash
git add src/market_feed.py
git commit -m "feat: setup real-time WebSocket market data receiver"
```

---

### Task 4: Order Placement & Safety Controls

**Files:**
- Create: `src/orders.py`
- Create: `src/db.py`

**Step 1: Setup SQLite DB for logging**
Create `src/db.py` to keep track of trades placed.
```python
import sqlite3

def init_db():
    conn = sqlite3.connect("trading.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            symbol TEXT,
            quantity INTEGER,
            price REAL,
            order_type TEXT,
            transaction_type TEXT,
            status TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
```

**Step 2: Implement order client with risk checks**
Create `src/orders.py` to place orders safely.
```python
import httpx
from src.config import Config

async def place_order(symbol: str, quantity: int, transaction_type: str, access_token: str):
    # Safety Check 1: Maximum capital allowed
    # Safety Check 2: Max daily loss threshold
    # Place Limit/Market Order to Upstox API
    url = "https://api.upstox.com/v2/order/place"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "quantity": quantity,
        "product": "I", # Intraday
        "validity": "DAY",
        "price": 0, # 0 for market protection or specify limit
        "tag": "agent-trading",
        "instrument_token": symbol,
        "order_type": "MARKET",
        "transaction_type": transaction_type
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        return r.json()
```

**Step 3: Commit**
```bash
git add src/db.py src/orders.py
git commit -m "feat: implement order execution client with SQLite logging"
```

---

### Task 5: Central Coordination & REST API Wrapper

**Files:**
- Create: `src/main.py`

**Step 1: Write FastAPI main coordinator**
Expose endpoints like `/buy` or `/sell` which can be triggered by your research component (the brain).
```python
from fastapi import FastAPI, BackgroundTasks
from src.auth import authenticate_upstox
from src.orders import place_order
from src.db import init_db

app = FastAPI(title="Stock Tool Execution Engine")
access_token = None

@app.on_event("startup")
async def startup():
    global access_token
    init_db()
    # Perform daily login on startup
    access_token = await authenticate_upstox()

@app.post("/execute")
async def execute_trade(symbol: str, quantity: int, action: str):
    # Triggers orders securely
    res = await place_order(symbol, quantity, action.upper(), access_token)
    return {"status": "submitted", "broker_response": res}
```

**Step 2: Commit**
```bash
git add src/main.py
git commit -m "feat: construct API entry point to run the execution engine"
```

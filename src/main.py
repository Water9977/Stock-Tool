import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from src.config import Config
from src.db import init_db, start_day_stats, get_connection, get_realized_pnl
from src.auth import authenticate_upstox
from src.market_feed import MarketFeed
from src.orders import place_order, monitor_virtual_positions, sync_open_positions
from src.technicals import TechnicalEngine
from src.brain import BrainEngine

app = FastAPI(title="Stock Tool Execution Engine", description="Minimalistic, low-latency automated trading bridge for Upstox")

# Cache to store runtime session token and engines
state = {
    "access_token": None,
    "trading_allowed": True,
    "technicals": None,
    "brain": None,
    "feed": None
}

class OrderRequest(BaseModel):
    symbol: str = Field(..., description="The broker instrument token, e.g., 'NSE_EQ|INE002A01018'")
    quantity: int = Field(..., gt=0, description="Quantity of shares to buy or sell")
    action: str = Field(..., description="Transaction action: 'BUY' or 'SELL'")
    target_pct: float = Field(default=0.02, description="Target Profit Percentage (e.g. 0.02 for 2%)")
    sl_pct: float = Field(default=0.01, description="Stop Loss Percentage (e.g. 0.01 for 1%)")

async def automated_execution_loop(feed: MarketFeed, technicals: TechnicalEngine, brain: BrainEngine):
    """The core decision engine connecting Hot, Warm, and Cold paths."""
    print("Starting Automated Execution Loop...")
    
    # Get a subscriber queue from Hot Path for Technicals
    tick_queue = feed.subscribe()
    
    # 2. Start Warm Path (Technicals)
    asyncio.create_task(technicals.run(tick_queue))
    
    # 3. Start Cold Path (Brain)
    asyncio.create_task(brain.poll_rss_feeds())
    asyncio.create_task(brain.run())
    
    # Execution Loop (Runs in main thread logic) to avoid spamming orders
    last_trade = {}
    
    while True:
        await asyncio.sleep(1.0) # Evaluator loop runs every second
        
        # Only trade if system is allowed
        if not state["trading_allowed"]:
            continue
            
        for yf_symbol in brain.watch_symbols:
            base_symbol = yf_symbol.replace(".NS", "")
            
            # Find the corresponding Upstox symbol from price_cache
            upstox_symbol = None
            for s in feed.price_cache.keys():
                if base_symbol in s:
                    upstox_symbol = s
                    break
                    
            if not upstox_symbol:
                continue
                
            # 1. Check Technical Signal (Warm Path)
            signal = technicals.get_signal(upstox_symbol)
            if signal == "NEUTRAL":
                continue
                
            # 2. Check AI Gate (Cold Path)
            gate = brain.get_gate_status(base_symbol)
            
            # Match Signal with Gate
            if signal == "BUY" and gate in ["ALLOW_BUY", "ALLOW_ALL"]:
                if last_trade.get(upstox_symbol, 0) > asyncio.get_event_loop().time() - 300: # 5 mins cooldown
                    continue
                last_trade[upstox_symbol] = asyncio.get_event_loop().time()
                print(f"[EXECUTION] Automated BUY triggered for {upstox_symbol} (Signal: {signal}, Gate: {gate})")
                await place_order(upstox_symbol, 10, "BUY", state["access_token"])
                
            elif signal == "SELL" and gate in ["ALLOW_SELL", "ALLOW_ALL"]:
                if last_trade.get(upstox_symbol, 0) > asyncio.get_event_loop().time() - 300:
                    continue
                last_trade[upstox_symbol] = asyncio.get_event_loop().time()
                print(f"[EXECUTION] Automated SELL triggered for {upstox_symbol} (Signal: {signal}, Gate: {gate})")
                await place_order(upstox_symbol, 10, "SELL", state["access_token"])

@app.on_event("startup")
async def startup_event():
    # 1. Initialize SQLite Database
    init_db()
    
    # 2. Setup today's starting stats (₹1,00,000 virtual balance)
    start_day_stats(100000.0)
    
    # 3. Retrieve daily access token (programmatic login)
    try:
        state["access_token"] = await authenticate_upstox()
    except Exception as e:
        print(f"CRITICAL: Failed to authenticate Upstox daily session: {e}")
        state["trading_allowed"] = False
        return

    # 4. Start WebSocket Market Feed cache
    feed = MarketFeed()
    await feed.start()
    state["feed"] = feed
    
    # 5. Sync Hot Path Cache for open positions
    sync_open_positions()
    
    # 6. Start Engines
    technicals = TechnicalEngine()
    brain = BrainEngine()
    state["technicals"] = technicals
    state["brain"] = brain
    
    # 7. Start background loops
    if Config.TRADING_MODE == "VIRTUAL":
        asyncio.create_task(monitor_virtual_positions())
        
    asyncio.create_task(automated_execution_loop(feed, technicals, brain))

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the real-time trading dashboard HTML page."""
    try:
        with open("src/static/index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load dashboard: {e}")

@app.get("/api/status")
async def status_api():
    """Returns the current trading status, realized metrics, signals, and macro headlines."""
    brain = state.get("brain")
    technicals = state.get("technicals")
    
    return {
        "status": "active" if state["trading_allowed"] else "suspended",
        "trading_mode": Config.TRADING_MODE,
        "today_realized_pnl": get_realized_pnl(),
        "max_allowed_loss": Config.MAX_DAILY_LOSS,
        "max_capital_per_trade": Config.MAX_CAPITAL_PER_TRADE,
        "gates": brain.gates if brain else {},
        "signals": technicals.signals if technicals else {},
        "global_headlines": brain.global_headlines if brain else []
    }

@app.post("/execute")
async def execute_order(request: OrderRequest):
    """Triggers an order execution via Upstox API or Simulator."""
    if not state["trading_allowed"]:
        raise HTTPException(status_code=400, detail="Trading is suspended or authentication failed today.")
        
    res = await place_order(
        symbol=request.symbol,
        quantity=request.quantity,
        transaction_type=request.action,
        access_token=state["access_token"],
        target_pct=request.target_pct,
        sl_pct=request.sl_pct
    )
    
    if res.get("status") == "REJECTED":
        raise HTTPException(status_code=400, detail=res.get("reason"))
        
    return res

@app.get("/portfolio")
async def get_portfolio():
    """Lists current open mock positions and closed trade history."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM orders WHERE status = 'VIRTUAL_OPEN'")
        columns = [column[0] for column in cursor.description]
        open_positions = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        cursor.execute("SELECT * FROM orders WHERE status != 'VIRTUAL_OPEN' ORDER BY timestamp DESC LIMIT 20")
        columns_hist = [column[0] for column in cursor.description]
        history = [dict(zip(columns_hist, row)) for row in cursor.fetchall()]
        
        return {
            "open_positions": open_positions,
            "trade_history": history
        }
    finally:
        conn.close()

@app.get("/prices")
async def get_latest_prices():
    """Retrieves current in-memory stock quotes."""
    feed = MarketFeed()
    return feed.price_cache

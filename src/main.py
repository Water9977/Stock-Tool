import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from src.config import Config
from src.db import init_db, start_day_stats, get_connection, get_realized_pnl
from src.auth import authenticate_upstox
from src.market_feed import MarketFeed
from src.orders import place_order, monitor_virtual_positions

app = FastAPI(title="Stock Tool Execution Engine", description="Minimalistic, low-latency automated trading bridge for Upstox")

# Cache to store runtime session token
state = {
    "access_token": None,
    "trading_allowed": True
}

class OrderRequest(BaseModel):
    symbol: str = Field(..., description="The broker instrument token, e.g., 'NSE_EQ|INE002A01018'")
    quantity: int = Field(..., gt=0, description="Quantity of shares to buy or sell")
    action: str = Field(..., description="Transaction action: 'BUY' or 'SELL'")
    target_pct: float = Field(default=0.02, description="Target Profit Percentage (e.g. 0.02 for 2%)")
    sl_pct: float = Field(default=0.01, description="Stop Loss Percentage (e.g. 0.01 for 1%)")

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
    
    # 5. Start background position monitor loop
    if Config.TRADING_MODE == "VIRTUAL":
        asyncio.create_task(monitor_virtual_positions())

@app.get("/")
async def status_dashboard():
    """Returns the current trading status and realized metrics."""
    return {
        "status": "active" if state["trading_allowed"] else "suspended",
        "trading_mode": Config.TRADING_MODE,
        "today_realized_pnl": get_realized_pnl(),
        "max_allowed_loss": Config.MAX_DAILY_LOSS,
        "max_capital_per_trade": Config.MAX_CAPITAL_PER_TRADE
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

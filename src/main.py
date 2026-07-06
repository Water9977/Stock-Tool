import asyncio
import os
import pytz
from contextlib import asynccontextmanager
from datetime import datetime, time
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from src.config import Config
from src.db import init_db, start_day_stats, start_day_stats_async, reset_simulation, get_connection, get_realized_pnl_async
from src.auth import authenticate_upstox
from src.market_feed import MarketFeed
from src.orders import place_order, monitor_open_positions, sync_open_positions, flatten_positions_for_symbol, clear_open_positions, risk_engine
from src.technicals import TechnicalEngine
from src.brain import BrainEngine

async def require_api_key(x_api_key: str | None = Header(default=None)):
    """
    Guards every mutating endpoint (order placement, DB reset). Fails closed: if API_KEY
    isn't configured, the endpoint refuses ALL requests rather than silently accepting
    unauthenticated orders/wipes on a public URL — this app is designed for public cloud
    deployment (Railway) with no other access control in front of it.
    """
    if not Config.API_KEY:
        raise HTTPException(status_code=503, detail="This endpoint is disabled: set the API_KEY environment variable to enable it.")
    if x_api_key != Config.API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")

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
    target_pct: float = Field(default=Config.TARGET_PCT, description="Target Profit Percentage (e.g. 0.01 for 1%)")
    sl_pct: float = Field(default=Config.SL_PCT, description="Stop Loss Percentage (e.g. 0.005 for 0.5%)")

async def automated_execution_loop(feed: MarketFeed, technicals: TechnicalEngine, brain: BrainEngine):
    """The core decision engine connecting Hot, Warm, and Cold paths."""
    print("Starting Automated Execution Loop...")
    
    # Get a subscriber queue from Hot Path for Technicals
    tick_queue = feed.subscribe()
    
    # 2. Start Warm Path (Technicals)
    asyncio.create_task(technicals.run(tick_queue))
    
    # 3. Start Cold Path (Brain)
    asyncio.create_task(brain.poll_rss_feeds())
    asyncio.create_task(brain.run(technicals))
    
    # Execution Loop (Runs in main thread logic) to avoid spamming orders
    last_trade = {}
    
    while True:
        await asyncio.sleep(1.0) # Evaluator loop runs every second
        
        # Only trade if system is allowed
        if not state["trading_allowed"]:
            continue
            
        # Check Indian Standard Time (IST) market hours guard
        if not Config.is_market_hours():
            continue

        feed_healthy = feed.is_feed_healthy()

        for yf_symbol in brain.watch_symbols:
            base_symbol = yf_symbol.replace(".NS", "")

            # Find the corresponding Upstox symbol from Config.SYMBOL_MAP
            upstox_symbol = Config.SYMBOL_MAP.get(yf_symbol)
            if not upstox_symbol:
                continue

            # 1. Check AI Gate (Cold Path)
            gate = brain.gates.get(base_symbol, "VETO")

            # FLATTEN is an emergency exit (fraud/crash signal from the AI) and deliberately
            # bypasses the feed-health gate below — it exists specifically to get out fast on
            # bad news, and blocking it during exactly the kind of chaos that might correlate
            # with feed instability would defeat its purpose. In LIVE mode this is also safe:
            # the exit is a MARKET order that fills at whatever the real broker price actually
            # is, not our local (possibly stale) cache — reconcile_live_order() picks up the
            # real fill price regardless.
            if gate == "FLATTEN":
                if not feed_healthy:
                    print(f"[FLATTEN] Executing emergency exit for {upstox_symbol} despite a stale feed (fraud/crash override).")
                await flatten_positions_for_symbol(upstox_symbol, state["access_token"])
                continue

            # Do not open new entries on a stale feed (e.g. every REST poll failing on an
            # expired token) — a frozen price can look like a false RSI/MACD extreme, and the
            # first real price after reconnecting is a gap-jump, not a signal.
            if not feed_healthy:
                continue

            # 2. Check Technical Signal (Warm Path)
            signal = technicals.get_signal(upstox_symbol)
            if signal == "NEUTRAL":
                continue
                
            # Match Signal with Gate
            if signal in ["BUY", "SELL"]:
                if last_trade.get(upstox_symbol, 0) > asyncio.get_running_loop().time() - 120: # 2 min cooldown per symbol
                    continue
                
                # Get current price to calculate dynamic quantity
                current_price = state["feed"].get_price(upstox_symbol)
                if not current_price or current_price <= 0:
                    continue
                    
                # Calculate dynamic quantity based on available balance and trade capital limit.
                # get_available_balance() does synchronous SQLite reads and must not run
                # directly on the event loop (this runs once per candidate symbol per second).
                from src.orders import get_available_balance
                available_balance = await asyncio.to_thread(get_available_balance)
                trade_capital = min(Config.MAX_CAPITAL_PER_TRADE, available_balance)
                qty = int(trade_capital / current_price)
                
                if qty <= 0:
                    print(f"[EXECUTION] Signal {signal} for {upstox_symbol} bypassed: insufficient balance (Price: Rs.{current_price:.2f}, Available: Rs.{available_balance:.2f})")
                    continue

                ai_reason = brain.reasoning.get(base_symbol, "").strip()
                trade_reasoning = (
                    f"Technical: {signal} signal on RSI/MACD. "
                    f"AI: {ai_reason if ai_reason else 'no commentary this cycle'}. "
                    f"Qty {qty} sized to Rs.{trade_capital:,.0f} of Rs.{Config.MAX_CAPITAL_PER_TRADE:,.0f} per-trade cap at Rs.{current_price:.2f}/share."
                )

                # Cooldown is only set on an accepted order below — a REJECTED order
                # (insufficient funds discovered inside place_order, daily-loss breaker,
                # etc.) would otherwise needlessly block this symbol from retrying for a
                # full 2 minutes even though no trade actually happened.
                if signal == "BUY" and gate in ["ALLOW_BUY", "ALLOW_ALL"]:
                    print(f"[EXECUTION] Automated BUY triggered for {upstox_symbol} (Signal: {signal}, Gate: {gate}, Qty: {qty})")
                    res = await place_order(upstox_symbol, qty, "BUY", state["access_token"], reasoning=trade_reasoning)
                    if res.get("status") in ("FILLED", "SUBMITTED"):
                        last_trade[upstox_symbol] = asyncio.get_running_loop().time()

                elif signal == "SELL" and gate in ["ALLOW_SELL", "ALLOW_ALL"]:
                    print(f"[EXECUTION] Automated SELL triggered for {upstox_symbol} (Signal: {signal}, Gate: {gate}, Qty: {qty})")
                    res = await place_order(upstox_symbol, qty, "SELL", state["access_token"], reasoning=trade_reasoning)
                    if res.get("status") in ("FILLED", "SUBMITTED"):
                        last_trade[upstox_symbol] = asyncio.get_running_loop().time()
async def shutdown_scheduler():
    """
    Monitors the operational time. 
    In VIRTUAL mode: Automatically flattens positions and suspends trading after 5 hours.
    In PAPER / LIVE mode: Flattens positions and suspends trading at 15:25 IST (market close).
    """
    if Config.TRADING_MODE == "VIRTUAL":
        runtime_seconds = Config.VIRTUAL_RUNTIME_HOURS * 3600
        print(f"[SYSTEM] {Config.VIRTUAL_RUNTIME_HOURS}-hour virtual operational timer started.")
        await asyncio.sleep(runtime_seconds)
        print(f"[SYSTEM] {Config.VIRTUAL_RUNTIME_HOURS} hours elapsed. Initiating automated position flattening and suspension...")
    else:
        ist = pytz.timezone("Asia/Kolkata")
        print("[SYSTEM] Market-close auto-flatten scheduler started (active for 15:25 IST on weekdays).")
        while True:
            await asyncio.sleep(10.0)  # Check every 10 seconds
            now = datetime.now(ist)
            
            # Check if weekday and time is at or past 15:25 IST
            if now.weekday() < 5 and now.time() >= time(15, 25):
                print(f"[SYSTEM] Current time {now.strftime('%H:%M:%S')} is at or past 15:25 IST. Initiating end-of-day flatten...")
                break
                
    # 1. Suspend trading so no new orders are placed
    state["trading_allowed"] = False
    
    # 2. Flush / Flatten all active watchlist positions
    brain = state["brain"]
    if brain:
        for yf_symbol in brain.watch_symbols:
            upstox_symbol = Config.SYMBOL_MAP.get(yf_symbol)
            if upstox_symbol:
                print(f"[SYSTEM] Auto-flattening {upstox_symbol} before suspension.")
                await flatten_positions_for_symbol(upstox_symbol, state["access_token"])
                
    print("[SYSTEM] Automated trading suspended. Final trade logs are locked on the dashboard.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # @app.on_event("startup") is deprecated in current FastAPI; this is the replacement.
    # 1. Initialize SQLite Database
    init_db()

    # 2. If RESET_ON_START=true, wipe all previous trade history first
    if os.getenv("RESET_ON_START", "false").lower() == "true":
        reset_simulation()
        print("[SYSTEM] RESET_ON_START=true: clean slate started.")

    # 3. Setup today's starting stats (loaded from config)
    await start_day_stats_async(Config.STARTING_CAPITAL)

    # 3b. Restore circuit-breaker loss counter so a restart mid-streak does not reset it
    await asyncio.to_thread(risk_engine.load_from_db)

    # 3. Retrieve daily access token (programmatic login)
    try:
        state["access_token"] = await authenticate_upstox()
    except Exception as e:
        print(f"CRITICAL: Failed to authenticate Upstox daily session: {e}")
        state["trading_allowed"] = False

    # A lifespan context manager must yield exactly once — a bare `return` here (as the old
    # @app.on_event("startup") handler used) would skip the yield entirely and break the
    # ASGI lifespan protocol. Steps 4-8 only make sense if authentication succeeded above.
    if state["trading_allowed"]:
        # 4. Start WebSocket Market Feed cache
        feed = MarketFeed()
        feed.set_access_token(state["access_token"])  # Wire token for LIVE mode
        await feed.start()
        state["feed"] = feed

        # 5. Sync Hot Path Cache for open positions
        sync_open_positions()

        # 6. Start Engines
        technicals = TechnicalEngine()
        brain = BrainEngine()
        state["technicals"] = technicals
        state["brain"] = brain

        # 6b. Pre-warm indicators from historical candles BEFORE any live-tick consumer task
        # exists, so there's no race writing into the same RSI/MACD objects from two places
        # at once. This must complete (not just be scheduled) before automated_execution_loop
        # is created below, since that's what spins up technicals.run(tick_queue).
        await technicals.seed_from_history(state.get("access_token"))

        # 7. Start background loops
        asyncio.create_task(monitor_open_positions(state.get("access_token")))

        asyncio.create_task(automated_execution_loop(feed, technicals, brain))

        # 8. Start 5-hour automatic shutdown timer
        asyncio.create_task(shutdown_scheduler())

    yield
    # No shutdown-phase cleanup currently — background tasks are daemon-like asyncio tasks
    # that end when the process exits.

app = FastAPI(title="Stock Tool Execution Engine", description="Minimalistic, low-latency automated trading bridge for Upstox", lifespan=lifespan)

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

    today_pnl = await get_realized_pnl_async()

    from src.orders import get_available_balance
    from src.db import get_starting_capital
    wallet_balance = await asyncio.to_thread(get_available_balance)
    starting_capital = await asyncio.to_thread(get_starting_capital)

    return {
        "status": "active" if state["trading_allowed"] else "suspended",
        "trading_mode": Config.TRADING_MODE,
        "today_realized_pnl": today_pnl,
        "max_allowed_loss": Config.MAX_DAILY_LOSS,
        "max_capital_per_trade": Config.MAX_CAPITAL_PER_TRADE,
        "starting_capital": starting_capital,
        "wallet_balance": wallet_balance,
        "gates": brain.gates if brain else {},
        "signals": technicals.signals if technicals else {},
        "global_headlines": brain.global_headlines if brain else [],
        "market_hours_open": Config.is_market_hours(),
        "feed_healthy": state["feed"].is_feed_healthy() if state.get("feed") else False
    }

@app.get("/api/symbols")
async def symbols_api():
    """Returns watchlist symbol metadata (instrument key -> display name) so the frontend
    never has to hardcode the watchlist independently of Config.SYMBOL_MAP."""
    return {
        instrument_key: {"name": yf_symbol.replace(".NS", ""), "yf": yf_symbol}
        for yf_symbol, instrument_key in Config.SYMBOL_MAP.items()
    }

@app.post("/execute", dependencies=[Depends(require_api_key)])
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

def _get_portfolio_sync():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM orders WHERE status IN ('VIRTUAL_OPEN', 'LIVE_OPEN')")
        columns = [column[0] for column in cursor.description]
        open_positions = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute("SELECT * FROM orders WHERE status NOT IN ('VIRTUAL_OPEN', 'LIVE_OPEN') ORDER BY timestamp DESC LIMIT 20")
        columns_hist = [column[0] for column in cursor.description]
        history = [dict(zip(columns_hist, row)) for row in cursor.fetchall()]
        
        return {
            "open_positions": open_positions,
            "trade_history": history
        }
    finally:
        conn.close()

@app.get("/portfolio")
async def get_portfolio():
    """Lists current open mock positions and closed trade history."""
    return await asyncio.to_thread(_get_portfolio_sync)


@app.get("/prices")
async def get_latest_prices():
    """Retrieves current in-memory stock quotes."""
    feed = MarketFeed()
    return feed.price_cache


@app.post("/admin/reset-simulation", dependencies=[Depends(require_api_key)])
async def reset_simulation_endpoint():
    """Wipes all virtual trades and resets simulation."""
    if Config.TRADING_MODE == "LIVE":
        raise HTTPException(status_code=403, detail="Refusing to reset simulation in LIVE mode — this would wipe the local DB while real broker positions could still be open and untracked.")
    await asyncio.to_thread(reset_simulation)
    clear_open_positions()
    await start_day_stats_async(Config.STARTING_CAPITAL)
    return {"status": "reset", "starting_capital": Config.STARTING_CAPITAL}


class SetCapitalRequest(BaseModel):
    amount: float = Field(..., gt=0, description="New value for TODAY's starting_capital row.")

@app.post("/admin/set-starting-capital", dependencies=[Depends(require_api_key)])
async def set_starting_capital_endpoint(req: SetCapitalRequest):
    """
    Directly corrects today's starting_capital without touching realized_pnl, open
    positions, or trade history. Exists for exactly the case this was first needed for:
    daily_stats compounds forward from the previous day's ending balance now, but a day's
    row that was already created before that fix landed can't retroactively benefit from
    it — this lets that one row be corrected by hand instead of wiping real trade history
    via /admin/reset-simulation just to fix an accounting figure.
    """
    def _set(cursor):
        from src.db import _ist_today_str, _ensure_day_row
        _ensure_day_row(cursor, req.amount)
        cursor.execute("UPDATE daily_stats SET starting_capital = ? WHERE date = ?", (req.amount, _ist_today_str()))

    def _run():
        conn = get_connection()
        cursor = conn.cursor()
        try:
            _set(cursor)
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_run)
    from src.orders import get_available_balance
    new_balance = await asyncio.to_thread(get_available_balance)
    return {"status": "updated", "starting_capital": req.amount, "wallet_balance": new_balance}


class BacktestRequest(BaseModel):
    symbol: str = "SBIN.NS"
    interval: str = "5m"
    period: str = "5d"


_VALID_BACKTEST_INTERVALS = {"1m", "5m", "15m", "1h", "1d"}
_VALID_BACKTEST_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y"}
_backtest_lock = asyncio.Lock()

@app.post("/backtest")
async def trigger_backtest(req: BacktestRequest):
    """
    Runs a historical backtest for the given symbol and returns P&L metrics.
    Accepts Yahoo Finance ticker symbols (e.g. 'RELIANCE.NS', 'TCS.NS').
    Supported intervals: 1m, 5m, 15m, 1h, 1d
    Supported periods : 1d, 5d, 1mo, 3mo, 6mo, 1y

    This endpoint is intentionally left open to the dashboard's own Backtest Lab tab (which
    has no API-key mechanism of its own), unlike /execute and /admin/reset-simulation. It is
    read-only against Yahoo Finance and never touches the trading account or DB, so the
    actual risk here is denial-of-service via unbounded downloads, not fund loss — bounded
    below via an interval/period allowlist and a single-concurrent-run lock instead.
    """
    symbol = req.symbol
    interval = req.interval
    period = req.period

    if interval not in _VALID_BACKTEST_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'. Must be one of: {sorted(_VALID_BACKTEST_INTERVALS)}")
    if period not in _VALID_BACKTEST_PERIODS:
        raise HTTPException(status_code=400, detail=f"Invalid period '{period}'. Must be one of: {sorted(_VALID_BACKTEST_PERIODS)}")
    if _backtest_lock.locked():
        raise HTTPException(status_code=429, detail="A backtest is already running. Try again shortly.")

    async with _backtest_lock:
        return await _run_backtest(symbol, interval, period)


async def _run_backtest(symbol: str, interval: str, period: str):
    # Delegates to src/backtest.py's shared download_history()/replay_ticks() — this endpoint
    # used to reimplement the exact same replay loop independently of the CLI backtester,
    # which meant the two would silently drift apart on the next change to either one.
    from src.backtest import download_history, replay_ticks, SYMBOL_MAP

    try:
        upstox_symbol = SYMBOL_MAP.get(symbol, f"NSE_EQ|{symbol.replace('.NS', '')}")
        df = await download_history(symbol, interval, period)
        if df.empty:
            raise HTTPException(status_code=400, detail="No historical data found for this symbol/period.")

        result = await replay_ticks(df, symbol, upstox_symbol)

        total = result.wins + result.losses
        win_rate = round(result.wins / total * 100, 1) if total else 0.0
        return {
            "symbol": symbol,
            "interval": interval,
            "period": period,
            "note": "Technical-signal-only backtest. Does NOT apply the live system's AI gate "
                    "(ALLOW_BUY/ALLOW_SELL/ALLOW_ALL/VETO/FLATTEN) — treat this as an upper "
                    "bound on trade frequency; the live system trades less often and differently "
                    "since the AI gate can additionally veto or flatten any of these signals.",
            "total_trades": total,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate_pct": win_rate,
            "total_gross_pnl": round(result.total_gross_pnl, 2),
            "total_charges": round(result.total_charges, 2),
            "total_pnl": round(result.total_net_pnl, 2),
            "trades": result.trades[-20:]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backtest failed: {str(e)}")

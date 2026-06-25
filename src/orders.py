import asyncio
import uuid
import sqlite3
from src.config import Config
from src.db import get_connection, get_realized_pnl, update_realized_pnl
from src.market_feed import MarketFeed

async def place_order(symbol: str, quantity: int, transaction_type: str, access_token: str, target_pct: float = 0.02, sl_pct: float = 0.01) -> dict:
    """
    Places a virtual or live trade.
    Enforces daily loss limits and capital per trade constraints.
    """
    transaction_type = transaction_type.upper()
    
    # 1. Enforce Max Daily Loss safety gate
    current_pnl = get_realized_pnl()
    if current_pnl <= -Config.MAX_DAILY_LOSS:
        return {
            "status": "REJECTED",
            "reason": f"Trading suspended for today. Realized loss ({current_pnl}) exceeded limit (-{Config.MAX_DAILY_LOSS})"
        }
        
    # 2. Get current price
    feed = MarketFeed()
    current_price = feed.get_price(symbol)
    trade_value = current_price * quantity
    
    # 3. Enforce maximum capital per trade limits
    if trade_value > Config.MAX_CAPITAL_PER_TRADE:
        return {
            "status": "REJECTED",
            "reason": f"Trade value ({trade_value}) exceeds maximum allowed capital per trade ({Config.MAX_CAPITAL_PER_TRADE})"
        }
        
    if Config.TRADING_MODE == "VIRTUAL":
        order_id = f"V-{uuid.uuid4().hex[:8].upper()}"
        
        # Calculate target and stop loss prices
        if transaction_type == "BUY":
            target_price = round(current_price * (1 + target_pct), 2)
            stop_loss_price = round(current_price * (1 - sl_pct), 2)
        else:  # SELL (Shorting)
            target_price = round(current_price * (1 - target_pct), 2)
            stop_loss_price = round(current_price * (1 + sl_pct), 2)
            
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO orders (id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, symbol, quantity, current_price, target_price, stop_loss_price, "MARKET", transaction_type, "VIRTUAL_OPEN")
            )
            conn.commit()
        finally:
            conn.close()
            
        print(f"Placed VIRTUAL trade: {transaction_type} {quantity} shares of {symbol} at Rs.{current_price}. Target: Rs.{target_price}, Stop Loss: Rs.{stop_loss_price}")
        return {
            "status": "FILLED",
            "order_id": order_id,
            "symbol": symbol,
            "price": current_price,
            "quantity": quantity,
            "mode": "VIRTUAL"
        }
    else:
        # LIVE Trading order placement
        import httpx
        url = "https://api.upstox.com/v2/order/place"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        
        # Format payload according to Upstox API
        payload = {
            "quantity": quantity,
            "product": "I", # Intraday
            "validity": "DAY",
            "price": 0.0,
            "tag": "agent-trade",
            "instrument_token": symbol,
            "order_type": "MARKET",
            "transaction_type": transaction_type
        }
        
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                return {"status": "FAILED", "reason": r.text}
                
            res = r.json()
            order_id = res["data"]["order_id"]
            
            # Log to local DB
            conn = get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO orders (id, symbol, quantity, price, order_type, transaction_type, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (order_id, symbol, quantity, current_price, "MARKET", transaction_type, "LIVE_SUBMITTED")
                )
                conn.commit()
            finally:
                conn.close()
                
            return {"status": "SUBMITTED", "order_id": order_id, "mode": "LIVE"}

async def monitor_virtual_positions():
    """Background loop that watches open virtual positions and resolves target / stop losses."""
    feed = MarketFeed()
    print("Starting background virtual position monitor...")
    
    while True:
        await asyncio.sleep(1.0)
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, symbol, quantity, price, target, stop_loss, transaction_type FROM orders WHERE status = 'VIRTUAL_OPEN'")
            open_positions = cursor.fetchall()
            
            for order_id, symbol, quantity, entry_price, target, stop_loss, transaction_type in open_positions:
                current_price = feed.get_price(symbol)
                trade_pnl = 0.0
                triggered = False
                trigger_reason = ""

                if transaction_type == "BUY":
                    if current_price >= target:
                        triggered = True
                        trigger_reason = "Target Profit Hit"
                        trade_pnl = (target - entry_price) * quantity
                    elif current_price <= stop_loss:
                        triggered = True
                        trigger_reason = "Stop Loss Triggered"
                        trade_pnl = (stop_loss - entry_price) * quantity
                else:  # SELL (Short position)
                    if current_price <= target:
                        triggered = True
                        trigger_reason = "Target Profit Hit"
                        trade_pnl = (entry_price - target) * quantity
                    elif current_price >= stop_loss:
                        triggered = True
                        trigger_reason = "Stop Loss Triggered"
                        trade_pnl = (entry_price - stop_loss) * quantity

                if triggered:
                    trade_pnl = round(trade_pnl, 2)
                    cursor.execute(
                        "UPDATE orders SET status = 'VIRTUAL_CLOSED', price = ? WHERE id = ?",
                        (current_price, order_id)
                    )
                    conn.commit()
                    update_realized_pnl(trade_pnl)
                    print(f"[{trigger_reason}] Closed trade {order_id} for {symbol}. Entry: Rs.{entry_price}, Exit: Rs.{current_price}. Trade P&L: Rs.{trade_pnl:+}. Total realized today: Rs.{get_realized_pnl():+}")
        except Exception as e:
            print(f"Error in position monitor thread: {e}")
        finally:
            conn.close()

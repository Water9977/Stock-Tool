import asyncio
import uuid
import sqlite3
from src.config import Config
from src.db import get_connection, get_realized_pnl, update_realized_pnl
from src.market_feed import MarketFeed

# In-memory Hot Path cache for open virtual positions
_open_positions = {}

def sync_open_positions():
    """Initializes the Hot Path cache from SQLite on startup."""
    global _open_positions
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, symbol, quantity, price, target, stop_loss, transaction_type FROM orders WHERE status = 'VIRTUAL_OPEN'")
        for row in cursor.fetchall():
            order_id, symbol, quantity, entry_price, target, stop_loss, transaction_type = row
            _open_positions[order_id] = {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": entry_price,
                "target": target,
                "stop_loss": stop_loss,
                "transaction_type": transaction_type
            }
        print(f"Hot Path Cache Synced: {_open_positions}")
    finally:
        conn.close()

def calculate_transaction_charges(buy_price: float, sell_price: float, qty: int) -> float:
    """
    Calculates standard Upstox intraday equity transaction charges and govt taxes.
    Matches the exact logic from src/backtest.py.
    """
    buy_value = buy_price * qty
    sell_value = sell_price * qty
    turnover = buy_value + sell_value
    
    # 1. Brokerage: 0.05% of turnover or Rs. 20 (whichever is lower) per side
    buy_brokerage = min(0.0005 * buy_value, 20.0)
    sell_brokerage = min(0.0005 * sell_value, 20.0)
    total_brokerage = buy_brokerage + sell_brokerage
    
    # 2. STT (0.025% on sell side)
    stt = 0.00025 * sell_value
    
    # 3. Exchange Transaction Charges (0.00322% on total turnover)
    exchange_charges = 0.0000322 * turnover
    
    # 4. GST (18% on brokerage + exchange charges)
    gst = 0.18 * (total_brokerage + exchange_charges)
    
    # 5. SEBI Fee (0.0001% on total turnover)
    sebi_fee = 0.000001 * turnover
    
    # 6. Stamp Duty (0.003% on buy side)
    stamp_duty = 0.00003 * buy_value
    
    total_charges = total_brokerage + stt + exchange_charges + gst + sebi_fee + stamp_duty
    return round(total_charges, 2)

def get_available_balance() -> float:
    """
    Returns the current available balance for the virtual simulation.
    Starting Capital + Today's Realized P&L - Locked Margin in open positions.
    """
    from src.db import get_starting_capital, get_realized_pnl
    starting = get_starting_capital()
    pnl = get_realized_pnl()
    locked = sum(pos["entry_price"] * pos["quantity"] for pos in _open_positions.values())
    return max(0.0, starting + pnl - locked)

async def place_order(symbol: str, quantity: int, transaction_type: str, access_token: str, target_pct: float = 0.02, sl_pct: float = 0.01) -> dict:
    """
    Places a virtual or live trade.
    Enforces daily loss limits, available capital checks, and capital per trade constraints.
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
    if current_price is None or current_price <= 0:
        return {
            "status": "REJECTED",
            "reason": f"Could not retrieve price feed for {symbol} to place order."
        }
        
    if Config.TRADING_MODE in ["VIRTUAL", "PAPER"]:
        # Apply entry slippage penalty (+0.05% on BUY, -0.05% on SELL)
        if transaction_type == "BUY":
            executed_price = round(current_price * (1 + 0.0005), 2)
        else:  # SELL
            executed_price = round(current_price * (1 - 0.0005), 2)
            
        trade_value = executed_price * quantity
        
        # 3. Enforce maximum capital per trade limits
        if trade_value > Config.MAX_CAPITAL_PER_TRADE:
            return {
                "status": "REJECTED",
                "reason": f"Trade value ({trade_value:.2f}) exceeds maximum allowed capital per trade ({Config.MAX_CAPITAL_PER_TRADE})"
            }
            
        # 4. Check available virtual/paper balance
        available_balance = get_available_balance()
        if trade_value > available_balance:
            return {
                "status": "REJECTED",
                "reason": f"Insufficient virtual funds. Required: Rs.{trade_value:.2f}, Available: Rs.{available_balance:.2f}"
            }
            
        order_id = f"V-{uuid.uuid4().hex[:8].upper()}"
        
        # Calculate target and stop loss prices from executed price
        if transaction_type == "BUY":
            target_price = round(executed_price * (1 + target_pct), 2)
            stop_loss_price = round(executed_price * (1 - sl_pct), 2)
        else:  # SELL (Shorting)
            target_price = round(executed_price * (1 - target_pct), 2)
            stop_loss_price = round(executed_price * (1 + sl_pct), 2)
            
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO orders (id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, symbol, quantity, executed_price, target_price, stop_loss_price, "MARKET", transaction_type, "VIRTUAL_OPEN")
            )
            conn.commit()
            
            # Hot Path update
            _open_positions[order_id] = {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": executed_price,
                "target": target_price,
                "stop_loss": stop_loss_price,
                "transaction_type": transaction_type
            }
        finally:
            conn.close()
            
        print(f"Placed VIRTUAL trade: {transaction_type} {quantity} shares of {symbol} at Rs.{executed_price} (raw LTP: Rs.{current_price}). Target: Rs.{target_price}, Stop Loss: Rs.{stop_loss_price}")
        return {
            "status": "FILLED",
            "order_id": order_id,
            "symbol": symbol,
            "price": executed_price,
            "quantity": quantity,
            "mode": "VIRTUAL"
        }
    else:
        # LIVE Trading order placement (Upstox V3 API)
        trade_value = current_price * quantity
        
        # 3. Enforce maximum capital per trade limits
        if trade_value > Config.MAX_CAPITAL_PER_TRADE:
            return {
                "status": "REJECTED",
                "reason": f"Trade value ({trade_value:.2f}) exceeds maximum allowed capital per trade ({Config.MAX_CAPITAL_PER_TRADE})"
            }
            
        import httpx
        url = "https://api.upstox.com/v3/order/place"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Algo-Name": "AntigravityStockAgent"
        }
        
        # Format payload according to Upstox V3 API
        payload = {
            "quantity": quantity,
            "product": "I", # Intraday
            "validity": "DAY",
            "price": 0.0,
            "tag": "agent-trade",
            "instrument_token": symbol,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
            "slice": False
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
    """Hot Path Background loop: watches open virtual positions and resolves target / stop losses purely in-memory (O(1) execution time limit)."""
    feed = MarketFeed()
    print("Starting background virtual position monitor (Hot Path)...")
    
    while True:
        await asyncio.sleep(0.1) # Check 10 times a second
        
        closed_orders = []
        for order_id, pos in list(_open_positions.items()):
            current_price = feed.get_price(pos["symbol"])
            if current_price is None or current_price <= 0:
                continue
                
            target = pos["target"]
            stop_loss = pos["stop_loss"]
            entry_price = pos["entry_price"]
            quantity = pos["quantity"]
            transaction_type = pos["transaction_type"]
            symbol = pos["symbol"]
            
            trade_pnl = 0.0
            triggered = False
            trigger_reason = ""
            executed_exit_price = 0.0

            if transaction_type == "BUY":
                if current_price >= target:
                    triggered = True
                    trigger_reason = "Target Profit Hit"
                    # Exit slippage penalty (-0.05% on exit)
                    executed_exit_price = round(current_price * (1 - 0.0005), 2)
                    buy_price, sell_price = entry_price, executed_exit_price
                    gross_pnl = round((executed_exit_price - entry_price) * quantity, 2)
                elif current_price <= stop_loss:
                    triggered = True
                    trigger_reason = "Stop Loss Triggered"
                    # Exit slippage penalty (-0.05% on exit)
                    executed_exit_price = round(current_price * (1 - 0.0005), 2)
                    buy_price, sell_price = entry_price, executed_exit_price
                    gross_pnl = round((executed_exit_price - entry_price) * quantity, 2)
            else:  # SELL (Short position)
                if current_price <= target:
                    triggered = True
                    trigger_reason = "Target Profit Hit"
                    # Exit slippage penalty (+0.05% on exit)
                    executed_exit_price = round(current_price * (1 + 0.0005), 2)
                    buy_price, sell_price = executed_exit_price, entry_price
                    gross_pnl = round((entry_price - executed_exit_price) * quantity, 2)
                elif current_price >= stop_loss:
                    triggered = True
                    trigger_reason = "Stop Loss Triggered"
                    # Exit slippage penalty (+0.05% on exit)
                    executed_exit_price = round(current_price * (1 + 0.0005), 2)
                    buy_price, sell_price = executed_exit_price, entry_price
                    gross_pnl = round((entry_price - executed_exit_price) * quantity, 2)

            if triggered:
                # Remove from Hot Path immediately to free margin
                del _open_positions[order_id]
                charges = calculate_transaction_charges(buy_price, sell_price, quantity)
                trade_pnl = round(gross_pnl - charges, 2)
                closed_orders.append((order_id, executed_exit_price, trade_pnl, trigger_reason, symbol, entry_price, charges))
                
        # Resolve DB asynchronously to prevent blocking Hot Path
        if closed_orders:
            conn = get_connection()
            cursor = conn.cursor()
            try:
                for order_id, executed_exit_price, trade_pnl, trigger_reason, symbol, entry_price, charges in closed_orders:
                    cursor.execute(
                        "UPDATE orders SET status = 'VIRTUAL_CLOSED', price = ? WHERE id = ?",
                        (executed_exit_price, order_id)
                    )
                    update_realized_pnl(trade_pnl)
                    print(f"[{trigger_reason}] Closed trade {order_id} for {symbol}. Entry: Rs.{entry_price}, Exit (slipped): Rs.{executed_exit_price}. Gross P&L: Rs.{(trade_pnl+charges):+.2f}, Charges: Rs.{charges:.2f}, Net P&L: Rs.{trade_pnl:+.2f}. Total realized today: Rs.{get_realized_pnl():+.2f}")
                conn.commit()
            except Exception as e:
                print(f"Error updating DB for closed positions: {e}")
            finally:
                conn.close()

async def flatten_positions_for_symbol(symbol: str, access_token: str):
    """
    Closes all open positions for a given symbol immediately at current market price.
    Used for emergency FLATTEN signals from AI sentiment.
    """
    feed = MarketFeed()
    current_price = feed.get_price(symbol)
    if current_price is None or current_price <= 0:
        print(f"[FLATTEN] Could not get price for {symbol}, aborting flatten.")
        return

    if Config.TRADING_MODE in ["VIRTUAL", "PAPER"]:
        # Find all open virtual positions for this symbol
        to_close = []
        for order_id, pos in list(_open_positions.items()):
            if pos["symbol"] == symbol:
                to_close.append((order_id, pos))
        
        if not to_close:
            return
            
        conn = get_connection()
        cursor = conn.cursor()
        try:
            for order_id, pos in to_close:
                qty = pos["quantity"]
                entry = pos["entry_price"]
                direction = pos["transaction_type"]
                
                # Apply exit slippage on flatten
                if direction == "BUY":
                    executed_exit_price = round(current_price * (1 - 0.0005), 2)
                    buy_price, sell_price = entry, executed_exit_price
                    gross_pnl = round((executed_exit_price - entry) * qty, 2)
                else:  # SELL (short)
                    executed_exit_price = round(current_price * (1 + 0.0005), 2)
                    buy_price, sell_price = executed_exit_price, entry
                    gross_pnl = round((entry - executed_exit_price) * qty, 2)
                    
                charges = calculate_transaction_charges(buy_price, sell_price, qty)
                pnl = round(gross_pnl - charges, 2)
                
                # Remove from memory cache immediately
                del _open_positions[order_id]
                
                # Update DB
                cursor.execute(
                    "UPDATE orders SET status = 'VIRTUAL_CLOSED', price = ? WHERE id = ?",
                    (executed_exit_price, order_id)
                )
                update_realized_pnl(pnl)
                print(f"[FLATTEN] Emergency closed VIRTUAL position {order_id} for {symbol} at Rs.{executed_exit_price}. Gross P&L: Rs.{(pnl+charges):+.2f}, Charges: Rs.{charges:.2f}, Net P&L: Rs.{pnl:+.2f}")
            conn.commit()
        except Exception as e:
            print(f"[FLATTEN ERROR] Failed to close virtual positions: {e}")
        finally:
            conn.close()
    else:
        # LIVE Mode: Query SQLite for open submitted live trades to offset
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, quantity, transaction_type FROM orders WHERE symbol = ? AND status = 'LIVE_SUBMITTED'", (symbol,))
            rows = cursor.fetchall()
            for row in rows:
                order_id, quantity, direction = row
                opposite_action = "SELL" if direction == "BUY" else "BUY"
                print(f"[FLATTEN] Emergency placing offsetting LIVE order: {opposite_action} {quantity} shares of {symbol}")
                await place_order(symbol, quantity, opposite_action, access_token)
                
                # Mark as closed in local DB
                cursor.execute("UPDATE orders SET status = 'LIVE_CLOSED' WHERE id = ?", (order_id,))
            conn.commit()
        except Exception as e:
            print(f"[FLATTEN ERROR] Failed to close live positions: {e}")
        finally:
            conn.close()

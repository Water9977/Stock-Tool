import asyncio
import uuid
import httpx
from src.config import Config
from src.db import get_connection, get_realized_pnl, update_realized_pnl
from src.market_feed import MarketFeed

# In-memory Hot Path cache for open virtual and live positions
_open_positions = {}

class RiskEngine:
    """Deterministic risk management engine."""
    def __init__(self):
        self.consecutive_losses = 0

    def load_from_db(self):
        """Restores the loss counter from DB so a container restart does not reset the circuit breaker."""
        from src.db import get_consecutive_losses
        self.consecutive_losses = get_consecutive_losses()
        print(f"[RiskEngine] Restored consecutive_losses={self.consecutive_losses} from DB.")

    def evaluate_pre_trade(self) -> dict:
        current_pnl = get_realized_pnl()
        if current_pnl <= -Config.MAX_DAILY_LOSS:
            return {"allowed": False, "reason": f"Max Daily Loss (Rs.{current_pnl}) exceeded limit (-Rs.{Config.MAX_DAILY_LOSS})"}
        if self.consecutive_losses >= 3:
            return {"allowed": False, "reason": "Consecutive Loss Circuit Breaker triggered (3 losses in a row)."}
        return {"allowed": True, "reason": ""}

    def record_trade_result(self, pnl: float):
        """Updates the in-memory loss counter. DB persistence happens in the sync DB writers
        (which already hold a cursor) via persist_loss_counter()."""
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def persist_loss_counter(self, cursor):
        from src.db import set_consecutive_losses
        set_consecutive_losses(self.consecutive_losses, cursor=cursor)

risk_engine = RiskEngine()

def clear_open_positions():
    """Clears the Hot Path cache of open positions."""
    global _open_positions
    _open_positions.clear()

def sync_open_positions():
    """Initializes the Hot Path cache from SQLite on startup."""
    global _open_positions
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, symbol, quantity, price, target, stop_loss, transaction_type, status FROM orders WHERE status IN ('VIRTUAL_OPEN', 'LIVE_SUBMITTED')")
        for row in cursor.fetchall():
            order_id, symbol, quantity, entry_price, target, stop_loss, transaction_type, status = row
            _open_positions[order_id] = {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": entry_price,
                "target": target,
                "stop_loss": stop_loss,
                "transaction_type": transaction_type,
                "mode": "VIRTUAL" if status == "VIRTUAL_OPEN" else "LIVE"
            }
        print(f"Hot Path Cache Synced: {_open_positions}")
    finally:
        conn.close()

def calculate_transaction_charges(buy_price: float, sell_price: float, qty: int) -> float:
    buy_value = buy_price * qty
    sell_value = sell_price * qty
    turnover = buy_value + sell_value
    
    buy_brokerage = min(0.0005 * buy_value, 20.0)
    sell_brokerage = min(0.0005 * sell_value, 20.0)
    total_brokerage = buy_brokerage + sell_brokerage
    stt = 0.00025 * sell_value
    exchange_charges = 0.0000322 * turnover
    gst = 0.18 * (total_brokerage + exchange_charges)
    sebi_fee = 0.000001 * turnover
    stamp_duty = 0.00003 * buy_value
    
    total_charges = total_brokerage + stt + exchange_charges + gst + sebi_fee + stamp_duty
    return round(total_charges, 2)

def get_available_balance() -> float:
    from src.db import get_starting_capital, get_realized_pnl
    starting = get_starting_capital()
    pnl = get_realized_pnl()
    locked = sum(pos["entry_price"] * pos["quantity"] for pos in _open_positions.values())
    return max(0.0, starting + pnl - locked)

async def execute_live_order(symbol: str, quantity: int, transaction_type: str, access_token: str) -> dict:
    url = "https://api-hft.upstox.com/v3/order/place"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Algo-Name": "AntigravityStockAgent"
    }
    
    payload = {
        "quantity": quantity,
        "product": "I",
        "validity": "DAY",
        "price": 0.0,
        "tag": "agent-trade",
        "instrument_token": symbol,
        "order_type": "MARKET",
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0.0,
        "is_amo": False,
        "slice": False,
        "market_protection": -1
    }
    
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            return {"status": "FAILED", "reason": r.text}
        res = r.json()
        if "data" not in res or "order_id" not in res["data"]:
            return {"status": "FAILED", "reason": str(res)}
        return {"status": "SUBMITTED", "order_id": res["data"]["order_id"]}

def _insert_order_sync(order_id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO orders (id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status)
        )
        conn.commit()
    finally:
        conn.close()

async def place_order(symbol: str, quantity: int, transaction_type: str, access_token: str, target_pct: float = 0.02, sl_pct: float = 0.01) -> dict:
    transaction_type = transaction_type.upper()
    
    risk_check = await asyncio.to_thread(risk_engine.evaluate_pre_trade)
    if not risk_check["allowed"]:
        return {"status": "REJECTED", "reason": risk_check["reason"]}
        
    feed = MarketFeed()
    current_price = feed.get_price(symbol)
    if current_price is None or current_price <= 0:
        return {"status": "REJECTED", "reason": f"Could not retrieve price feed for {symbol}"}
        
    trade_value = current_price * quantity
    if trade_value > Config.MAX_CAPITAL_PER_TRADE:
        return {"status": "REJECTED", "reason": f"Trade value ({trade_value:.2f}) exceeds max allowed ({Config.MAX_CAPITAL_PER_TRADE})"}
        
    available_balance = await asyncio.to_thread(get_available_balance)
    if trade_value > available_balance:
        return {"status": "REJECTED", "reason": f"Insufficient funds. Required: Rs.{trade_value:.2f}, Available: Rs.{available_balance:.2f}"}

    if transaction_type == "BUY":
        executed_price = round(current_price * (1 + 0.0005), 2)
        target_price = round(executed_price * (1 + target_pct), 2)
        stop_loss_price = round(executed_price * (1 - sl_pct), 2)
    else:
        executed_price = round(current_price * (1 - 0.0005), 2)
        target_price = round(executed_price * (1 - target_pct), 2)
        stop_loss_price = round(executed_price * (1 + sl_pct), 2)

    if Config.TRADING_MODE in ["VIRTUAL", "PAPER"]:
        order_id = f"V-{uuid.uuid4().hex[:8].upper()}"
        await asyncio.to_thread(_insert_order_sync, order_id, symbol, quantity, executed_price, target_price, stop_loss_price, "MARKET", transaction_type, "VIRTUAL_OPEN")
        
        _open_positions[order_id] = {
            "symbol": symbol, "quantity": quantity, "entry_price": executed_price,
            "target": target_price, "stop_loss": stop_loss_price, "transaction_type": transaction_type, "mode": "VIRTUAL"
        }
            
        print(f"Placed VIRTUAL trade: {transaction_type} {quantity} shares of {symbol} at Rs.{executed_price}. Target: Rs.{target_price}, Stop Loss: Rs.{stop_loss_price}")
        return {"status": "FILLED", "order_id": order_id, "symbol": symbol, "price": executed_price, "quantity": quantity, "mode": "VIRTUAL"}
    else:
        res = await execute_live_order(symbol, quantity, transaction_type, access_token)
        if res["status"] != "SUBMITTED":
            return res
            
        order_id = res["order_id"]
        await asyncio.to_thread(_insert_order_sync, order_id, symbol, quantity, current_price, target_price, stop_loss_price, "MARKET", transaction_type, "LIVE_SUBMITTED")
        
        _open_positions[order_id] = {
            "symbol": symbol, "quantity": quantity, "entry_price": current_price,
            "target": target_price, "stop_loss": stop_loss_price, "transaction_type": transaction_type, "mode": "LIVE"
        }
            
        print(f"Placed LIVE trade: {transaction_type} {quantity} shares of {symbol} at Rs.{current_price}. Target: Rs.{target_price}, Stop Loss: Rs.{stop_loss_price}")
        return {"status": "SUBMITTED", "order_id": order_id, "mode": "LIVE"}

def _process_closed_orders_sync(closed_orders):
    if not closed_orders:
        return
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for order_id, executed_exit_price, trade_pnl, trigger_reason, symbol, entry_price, charges, mode in closed_orders:
            status = "VIRTUAL_CLOSED" if mode == "VIRTUAL" else "LIVE_CLOSED"
            cursor.execute("UPDATE orders SET status = ?, price = ? WHERE id = ?", (status, executed_exit_price, order_id))
            update_realized_pnl(trade_pnl, cursor=cursor)
            print(f"[{trigger_reason}] Closed {mode} trade {order_id} for {symbol}. Net P&L: Rs.{trade_pnl:+.2f}")
        risk_engine.persist_loss_counter(cursor)
        conn.commit()
    except Exception as e:
        print(f"Error updating DB for closed positions: {e}")
    finally:
        conn.close()

async def monitor_open_positions(access_token: str = None):
    feed = MarketFeed()
    print("Starting background position monitor (Hot Path)...")
    
    while True:
        await asyncio.sleep(0.1)
        
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
            mode = pos["mode"]
            
            trade_pnl = 0.0
            triggered = False
            trigger_reason = ""
            executed_exit_price = 0.0

            if transaction_type == "BUY":
                if current_price >= target:
                    triggered = True
                    trigger_reason = "Target Profit Hit"
                    executed_exit_price = round(current_price * (1 - 0.0005), 2)
                    buy_price, sell_price = entry_price, executed_exit_price
                    gross_pnl = round((executed_exit_price - entry_price) * quantity, 2)
                elif current_price <= stop_loss:
                    triggered = True
                    trigger_reason = "Stop Loss Triggered"
                    executed_exit_price = round(current_price * (1 - 0.0005), 2)
                    buy_price, sell_price = entry_price, executed_exit_price
                    gross_pnl = round((executed_exit_price - entry_price) * quantity, 2)
            else:
                if current_price <= target:
                    triggered = True
                    trigger_reason = "Target Profit Hit"
                    executed_exit_price = round(current_price * (1 + 0.0005), 2)
                    buy_price, sell_price = executed_exit_price, entry_price
                    gross_pnl = round((entry_price - executed_exit_price) * quantity, 2)
                elif current_price >= stop_loss:
                    triggered = True
                    trigger_reason = "Stop Loss Triggered"
                    executed_exit_price = round(current_price * (1 + 0.0005), 2)
                    buy_price, sell_price = executed_exit_price, entry_price
                    gross_pnl = round((entry_price - executed_exit_price) * quantity, 2)

            if triggered:
                if mode == "LIVE":
                    opposite_action = "SELL" if transaction_type == "BUY" else "BUY"
                    if access_token:
                        res = await execute_live_order(symbol, quantity, opposite_action, access_token)
                        if res["status"] != "SUBMITTED":
                            print(f"[ERROR] Failed to close LIVE position {order_id}: {res['reason']}")
                            continue
                    else:
                        print(f"[ERROR] Access token missing to close LIVE position {order_id}!")
                        continue

                del _open_positions[order_id]
                charges = calculate_transaction_charges(buy_price, sell_price, quantity)
                trade_pnl = round(gross_pnl - charges, 2)
                
                risk_engine.record_trade_result(trade_pnl)
                closed_orders.append((order_id, executed_exit_price, trade_pnl, trigger_reason, symbol, entry_price, charges, mode))
                
        if closed_orders:
            await asyncio.to_thread(_process_closed_orders_sync, closed_orders)

def _flatten_positions_db_sync(updates):
    if not updates: return
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for order_id, executed_exit_price, pnl, mode in updates:
            status = "VIRTUAL_CLOSED" if mode == "VIRTUAL" else "LIVE_CLOSED"
            cursor.execute("UPDATE orders SET status = ?, price = ? WHERE id = ?", (status, executed_exit_price, order_id))
            update_realized_pnl(pnl, cursor=cursor)
        risk_engine.persist_loss_counter(cursor)
        conn.commit()
    except Exception as e:
        print(f"[FLATTEN ERROR] DB Update failed: {e}")
    finally:
        conn.close()

async def flatten_positions_for_symbol(symbol: str, access_token: str):
    feed = MarketFeed()
    current_price = feed.get_price(symbol)
    if current_price is None or current_price <= 0:
        print(f"[FLATTEN] Could not get price for {symbol}, aborting flatten.")
        return

    to_close = []
    for order_id, pos in list(_open_positions.items()):
        if pos["symbol"] == symbol:
            to_close.append((order_id, pos))
    
    if not to_close:
        return
        
    db_updates = []
    for order_id, pos in to_close:
        qty = pos["quantity"]
        entry = pos["entry_price"]
        direction = pos["transaction_type"]
        mode = pos["mode"]
        
        if direction == "BUY":
            executed_exit_price = round(current_price * (1 - 0.0005), 2)
            buy_price, sell_price = entry, executed_exit_price
            gross_pnl = round((executed_exit_price - entry) * qty, 2)
        else:
            executed_exit_price = round(current_price * (1 + 0.0005), 2)
            buy_price, sell_price = executed_exit_price, entry
            gross_pnl = round((entry - executed_exit_price) * qty, 2)
            
        charges = calculate_transaction_charges(buy_price, sell_price, qty)
        pnl = round(gross_pnl - charges, 2)
        
        if mode == "LIVE":
            opposite_action = "SELL" if direction == "BUY" else "BUY"
            if access_token:
                res = await execute_live_order(symbol, qty, opposite_action, access_token)
                if res["status"] != "SUBMITTED":
                    print(f"[FLATTEN ERROR] Failed to close LIVE position {order_id}: {res['reason']}")
                    continue

        del _open_positions[order_id]
        risk_engine.record_trade_result(pnl)
        db_updates.append((order_id, executed_exit_price, pnl, mode))
        print(f"[FLATTEN] Emergency closed {mode} position {order_id} for {symbol}. Net P&L: Rs.{pnl:+.2f}")
        
    if db_updates:
        await asyncio.to_thread(_flatten_positions_db_sync, db_updates)

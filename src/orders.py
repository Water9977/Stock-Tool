import asyncio
import time
import uuid
import httpx
from src.config import Config
from src.db import get_connection, get_realized_pnl, update_realized_pnl
from src.market_feed import MarketFeed

# In-memory Hot Path cache for open virtual and live positions
_open_positions = {}

class OrderThrottle:
    """
    Token-bucket limiter on real order submissions to the broker. SEBI's Feb-4-2025 algo
    circular treats retail API trading below 10 orders/second as "regular"; a synchronized
    signal across the 40-symbol watchlist could otherwise burst well past that in a single
    1-second evaluator tick with nothing bounding it. Target 8/sec (comfortable margin under
    10) and queue rather than drop — a delayed order is better than a rejected one.
    """
    def __init__(self, max_per_second: float = 8.0):
        self.min_interval = 1.0 / max_per_second
        self._lock = asyncio.Lock()
        self._last_submit = 0.0

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._last_submit + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_submit = time.monotonic()

_order_throttle = OrderThrottle(max_per_second=8.0)

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
        cursor.execute("SELECT id, symbol, quantity, price, target, stop_loss, transaction_type, status FROM orders WHERE status IN ('VIRTUAL_OPEN', 'LIVE_OPEN')")
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
    # Snapshot before summing — this dict is mutated by monitor_open_positions and
    # place_order concurrently; iterating it directly can raise "dict changed size during
    # iteration" if a position closes/opens mid-sum.
    locked = sum(pos["entry_price"] * pos["quantity"] for pos in list(_open_positions.values()))
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

    # Global throttle across every real order submission (entries, exits, flattens all pass
    # through this one function) — see OrderThrottle docstring for why.
    await _order_throttle.acquire()

    async with httpx.AsyncClient(proxy=Config.UPSTOX_HTTP_PROXY) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            return {"status": "FAILED", "reason": r.text}
        res = r.json()
        # Upstox V3 returns data.order_ids as an ARRAY (order_id singular never exists — an
        # earlier version of this code checked for "order_id" and so treated every
        # successful order as FAILED even though it had already hit the exchange).
        order_ids = res.get("data", {}).get("order_ids")
        if not order_ids:
            return {"status": "FAILED", "reason": str(res)}
        # This submission is accepted-by-broker, not yet confirmed filled — the caller must
        # reconcile via reconcile_live_order() before trusting quantity/price/existence.
        return {"status": "SUBMITTED", "order_id": order_ids[0], "order_ids": order_ids}

# Terminal Upstox order states (per api-documentation/appendix/order-status). Anything not in
# either set is still in flight and must be polled again.
_ORDER_STATUS_FILLED = {"complete"}
_ORDER_STATUS_FAILED = {"rejected", "cancelled", "cancelled after market order", "not modified", "not cancelled"}

async def reconcile_live_order(order_id: str, access_token: str, max_wait: float = 10.0, poll_interval: float = 0.5) -> dict:
    """
    Polls GET /v2/order/details until the order reaches a terminal state or max_wait elapses.
    A "SUBMITTED" response from execute_live_order only means the broker accepted the
    request — it says nothing about whether the order actually filled, at what price, or
    for how much quantity. Every live entry/exit must be reconciled through this before the
    system trusts it enough to write a position or compute P&L from it.

    Returns one of:
      {"status": "FILLED", "avg_price": float, "filled_qty": int}
      {"status": "REJECTED", "reason": str}
      {"status": "UNRESOLVED", "filled_qty": int}   # still not terminal after max_wait;
                                                      # filled_qty may be >0 (partial fill)
    """
    url = "https://api.upstox.com/v2/order/details"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    deadline = asyncio.get_running_loop().time() + max_wait

    last_filled_qty = 0
    async with httpx.AsyncClient(timeout=5.0, proxy=Config.UPSTOX_HTTP_PROXY) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                r = await client.get(url, headers=headers, params={"order_id": order_id})
                if r.status_code == 200:
                    data = r.json().get("data", {})
                    status = (data.get("status") or "").lower()
                    last_filled_qty = data.get("filled_quantity", 0) or 0
                    if status in _ORDER_STATUS_FILLED:
                        return {
                            "status": "FILLED",
                            "avg_price": data.get("average_price", 0.0),
                            "filled_qty": last_filled_qty,
                        }
                    if status in _ORDER_STATUS_FAILED:
                        return {"status": "REJECTED", "reason": f"Order {order_id} ended in state '{status}'"}
                else:
                    print(f"[RECONCILE] order/details HTTP {r.status_code} for {order_id}: {r.text[:150]}")
            except Exception as e:
                print(f"[RECONCILE] Exception polling order {order_id}: {e}")
            await asyncio.sleep(poll_interval)

    print(f"[RECONCILE] Order {order_id} did not reach a terminal state within {max_wait}s "
          f"(last known filled_qty={last_filled_qty}). Treating as unresolved.")
    return {"status": "UNRESOLVED", "filled_qty": last_filled_qty}

def _insert_order_sync(order_id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status, reasoning=""):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO orders (id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status, reasoning) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, symbol, quantity, price, target, stop_loss, order_type, transaction_type, status, reasoning)
        )
        conn.commit()
    finally:
        conn.close()

async def place_order(symbol: str, quantity: int, transaction_type: str, access_token: str, target_pct: float = None, sl_pct: float = None, reasoning: str = "") -> dict:
    target_pct = Config.TARGET_PCT if target_pct is None else target_pct
    sl_pct = Config.SL_PCT if sl_pct is None else sl_pct
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

    def _derive_target_stop(fill_price: float):
        if transaction_type == "BUY":
            return round(fill_price * (1 + target_pct), 2), round(fill_price * (1 - sl_pct), 2)
        return round(fill_price * (1 - target_pct), 2), round(fill_price * (1 + sl_pct), 2)

    if Config.TRADING_MODE in ["VIRTUAL", "PAPER"]:
        # Simulated fill — the cache price plus a fixed slippage assumption is the closest
        # thing to a "fill price" that exists in these modes, so deriving target/stop from
        # it here is correct (there is no broker fill to reconcile against).
        executed_price = round(current_price * (1 + 0.0005), 2) if transaction_type == "BUY" else round(current_price * (1 - 0.0005), 2)
        target_price, stop_loss_price = _derive_target_stop(executed_price)

        order_id = f"V-{uuid.uuid4().hex[:8].upper()}"
        await asyncio.to_thread(_insert_order_sync, order_id, symbol, quantity, executed_price, target_price, stop_loss_price, "MARKET", transaction_type, "VIRTUAL_OPEN", reasoning)

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

        # Acceptance by the broker is not a fill. Reconcile against the real order book
        # before trusting quantity or price — target/stop must be derived from the actual
        # average fill price, not the pre-trade cache price, which can be stale or simply
        # wrong for a market order in a fast-moving instrument.
        recon = await reconcile_live_order(order_id, access_token)

        if recon["status"] == "REJECTED":
            await asyncio.to_thread(
                _insert_order_sync, order_id, symbol, quantity, current_price, 0.0, 0.0,
                "MARKET", transaction_type, "LIVE_REJECTED", reasoning
            )
            print(f"[LIVE] Order {order_id} for {symbol} was rejected/cancelled by the broker: {recon.get('reason')}")
            return {"status": "REJECTED", "reason": recon.get("reason", "Order rejected by broker"), "order_id": order_id}

        if recon["status"] == "UNRESOLVED" or recon.get("filled_qty", 0) <= 0:
            # Could not confirm a fill within the reconciliation window. Do NOT add this to
            # _open_positions — an untracked position with no monitoring is worse than a
            # position this system simply doesn't know about yet. Flag it loudly for manual
            # review rather than guessing.
            await asyncio.to_thread(
                _insert_order_sync, order_id, symbol, quantity, current_price, 0.0, 0.0,
                "MARKET", transaction_type, "LIVE_UNRESOLVED", reasoning
            )
            print(f"[LIVE ALERT] Order {order_id} for {symbol} could not be confirmed filled — "
                  f"check the broker terminal manually. This order is NOT being monitored by the bot.")
            return {"status": "UNRESOLVED", "order_id": order_id, "reason": "Fill could not be confirmed in time"}

        filled_qty = recon["filled_qty"]
        fill_price = recon["avg_price"] or current_price
        if filled_qty != quantity:
            print(f"[LIVE ALERT] Order {order_id} for {symbol} partially filled: requested {quantity}, "
                  f"got {filled_qty}. Tracking the position at the filled quantity only.")

        target_price, stop_loss_price = _derive_target_stop(fill_price)

        await asyncio.to_thread(
            _insert_order_sync, order_id, symbol, filled_qty, fill_price, target_price, stop_loss_price,
            "MARKET", transaction_type, "LIVE_OPEN", reasoning
        )

        _open_positions[order_id] = {
            "symbol": symbol, "quantity": filled_qty, "entry_price": fill_price,
            "target": target_price, "stop_loss": stop_loss_price, "transaction_type": transaction_type, "mode": "LIVE"
        }

        print(f"Placed LIVE trade: {transaction_type} {filled_qty} shares of {symbol} at Rs.{fill_price} (confirmed fill). Target: Rs.{target_price}, Stop Loss: Rs.{stop_loss_price}")
        return {"status": "FILLED", "order_id": order_id, "symbol": symbol, "price": fill_price, "quantity": filled_qty, "mode": "LIVE"}

def _process_closed_orders_sync(closed_orders):
    if not closed_orders:
        return
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for order_id, executed_exit_price, trade_pnl, trigger_reason, symbol, entry_price, charges, mode in closed_orders:
            status = "VIRTUAL_CLOSED" if mode == "VIRTUAL" else "LIVE_CLOSED"
            cursor.execute(
                "UPDATE orders SET status = ?, exit_price = ?, pnl = ? WHERE id = ?",
                (status, executed_exit_price, trade_pnl, order_id)
            )
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

        # Don't evaluate SL/TP against a stale or just-reconnected feed. A price frozen
        # during an outage (e.g. an expired token) can drift far from reality before the
        # feed recovers, and the first fresh tick afterwards is a gap-jump that would
        # otherwise force-close every open position at a price the account never actually
        # had a chance to react to.
        if not feed.is_feed_healthy():
            continue

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
                    if not access_token:
                        print(f"[ERROR] Access token missing to close LIVE position {order_id}!")
                        continue

                    res = await execute_live_order(symbol, quantity, opposite_action, access_token)
                    if res["status"] != "SUBMITTED":
                        # Genuinely failed to submit — position is still open at the broker,
                        # leave it in _open_positions so the next tick tries again.
                        print(f"[ERROR] Failed to submit exit order for LIVE position {order_id}: {res.get('reason')}")
                        continue

                    # Confirm the exit actually filled before treating this position as
                    # closed. Trusting "SUBMITTED" alone was the exact bug that caused a
                    # false-FAILED misread to retry the same exit every 100ms in production
                    # (H-2) — reconciling here, and only deleting from _open_positions once
                    # a fill is confirmed, closes that loop.
                    recon = await reconcile_live_order(res["order_id"], access_token)
                    if recon["status"] != "FILLED" or recon.get("filled_qty", 0) <= 0:
                        print(f"[LIVE ALERT] Exit order {res['order_id']} for {symbol} could not be confirmed "
                              f"filled ({recon['status']}) — position {order_id} left open for manual review.")
                        continue

                    executed_exit_price = recon["avg_price"] or executed_exit_price
                    if transaction_type == "BUY":
                        buy_price, sell_price = entry_price, executed_exit_price
                        gross_pnl = round((executed_exit_price - entry_price) * quantity, 2)
                    else:
                        buy_price, sell_price = executed_exit_price, entry_price
                        gross_pnl = round((entry_price - executed_exit_price) * quantity, 2)

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
            cursor.execute(
                "UPDATE orders SET status = ?, exit_price = ?, pnl = ? WHERE id = ?",
                (status, executed_exit_price, pnl, order_id)
            )
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
        else:
            executed_exit_price = round(current_price * (1 + 0.0005), 2)

        if mode == "LIVE":
            if not access_token:
                print(f"[FLATTEN ERROR] Access token missing to close LIVE position {order_id}!")
                continue

            opposite_action = "SELL" if direction == "BUY" else "BUY"
            res = await execute_live_order(symbol, qty, opposite_action, access_token)
            if res["status"] != "SUBMITTED":
                print(f"[FLATTEN ERROR] Failed to submit exit order for LIVE position {order_id}: {res.get('reason')}")
                continue

            # Same reconciliation discipline as monitor_open_positions: don't trust
            # "SUBMITTED" as "closed" — confirm the fill and use its real price.
            recon = await reconcile_live_order(res["order_id"], access_token)
            if recon["status"] != "FILLED" or recon.get("filled_qty", 0) <= 0:
                print(f"[FLATTEN ALERT] Exit order {res['order_id']} for {symbol} could not be confirmed "
                      f"filled ({recon['status']}) — position {order_id} left open for manual review.")
                continue
            executed_exit_price = recon["avg_price"] or executed_exit_price

        if direction == "BUY":
            buy_price, sell_price = entry, executed_exit_price
            gross_pnl = round((executed_exit_price - entry) * qty, 2)
        else:
            buy_price, sell_price = executed_exit_price, entry
            gross_pnl = round((entry - executed_exit_price) * qty, 2)

        charges = calculate_transaction_charges(buy_price, sell_price, qty)
        pnl = round(gross_pnl - charges, 2)

        del _open_positions[order_id]
        risk_engine.record_trade_result(pnl)
        db_updates.append((order_id, executed_exit_price, pnl, mode))
        print(f"[FLATTEN] Emergency closed {mode} position {order_id} for {symbol}. Net P&L: Rs.{pnl:+.2f}")
        
    if db_updates:
        await asyncio.to_thread(_flatten_positions_db_sync, db_updates)

def _update_position_targets_sync(updates):
    """updates: list of (order_id, new_target, new_stop_loss)"""
    if not updates:
        return
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for order_id, new_target, new_stop_loss in updates:
            cursor.execute("UPDATE orders SET target = ?, stop_loss = ? WHERE id = ?", (new_target, new_stop_loss, order_id))
        conn.commit()
    finally:
        conn.close()

async def adjust_open_positions_for_symbol(symbol: str, suggested_target: float = None, suggested_stop_loss: float = None) -> list:
    """
    Lets the Cold Path AI re-price the target/stop-loss on positions that are ALREADY open,
    not just gate new entries. Previously a target/stop was frozen the instant a trade opened
    — on a stock that moves slowly, that can mean the position sits locked for hours (or the
    rest of the day) waiting to hit a target that may never come, with its capital unusable
    for any other signal in the meantime.

    Every suggestion is validated server-side and can only TIGHTEN the position — move the
    target closer (lock in a smaller win sooner) and/or move the stop closer (protect more of
    the current gain) — never loosen it further away, regardless of what the AI suggests. A
    bad or manipulated LLM output should never be able to widen this account's risk on a
    position that's already live; the prompt asks for tightening only, but the guarantee here
    comes from this code, not the prompt.

    Returns the list of order_ids actually adjusted.
    """
    adjusted = []
    db_updates = []

    for order_id, pos in list(_open_positions.items()):
        if pos["symbol"] != symbol:
            continue

        direction = pos["transaction_type"]
        entry = pos["entry_price"]
        current_target = pos["target"]
        current_stop = pos["stop_loss"]
        new_target = current_target
        new_stop = current_stop

        if suggested_target is not None:
            if direction == "BUY":
                # Tightening a BUY target means pulling it DOWN toward entry (closer, smaller
                # win) but never below entry (that would turn a profit target into a loss exit).
                if entry < suggested_target < current_target:
                    new_target = suggested_target
            else:
                if entry > suggested_target > current_target:
                    new_target = suggested_target

        if suggested_stop_loss is not None:
            if direction == "BUY":
                # Tightening a BUY stop means raising it toward entry/current price (protects
                # more gain) but never above entry — that's a target adjustment, not a stop.
                if current_stop < suggested_stop_loss < entry:
                    new_stop = suggested_stop_loss
            else:
                if current_stop > suggested_stop_loss > entry:
                    new_stop = suggested_stop_loss

        if new_target != current_target or new_stop != current_stop:
            pos["target"] = new_target
            pos["stop_loss"] = new_stop
            db_updates.append((order_id, new_target, new_stop))
            adjusted.append(order_id)
            print(f"[ADJUST] {order_id} ({symbol}): target {current_target}->{new_target}, stop {current_stop}->{new_stop}")

    if db_updates:
        await asyncio.to_thread(_update_position_targets_sync, db_updates)

    return adjusted

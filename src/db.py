import sqlite3
import os
import asyncio
import pytz
from datetime import datetime

DATABASE_FILE = os.getenv("DATABASE_PATH", "trading.db")
IST = pytz.timezone("Asia/Kolkata")

def _ist_today_str() -> str:
    """
    Single definition of "today" for every daily_stats lookup. Previously each function used
    date.today() independently, which follows the server's local/system timezone (UTC on
    Railway) rather than IST — and more importantly, nothing ever created a new day's row
    except main.py's one-time startup_event call. On a 24/7 deployment that crosses midnight
    without a restart, every later "today" lookup found no matching row and silently
    degraded: update_realized_pnl became a no-op (P&L of trades closed after midnight was
    lost), get_realized_pnl returned 0.0 (masking the daily-loss circuit breaker), and
    get_starting_capital fell back to a hardcoded 2000.0 regardless of Config.STARTING_CAPITAL.
    """
    return datetime.now(IST).date().isoformat()

def get_connection():
    return sqlite3.connect(DATABASE_FILE, timeout=10)

def _init_db_sync():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            target REAL,
            stop_loss REAL,
            order_type TEXT NOT NULL,
            transaction_type TEXT NOT NULL,
            status TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            starting_capital REAL NOT NULL,
            realized_pnl REAL DEFAULT 0.0,
            consecutive_losses INTEGER DEFAULT 0
        )
    """)
    # Migration for pre-existing DBs created before consecutive_losses column existed.
    try:
        cursor.execute("ALTER TABLE daily_stats ADD COLUMN consecutive_losses INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration for pre-existing DBs created before exit_price/pnl columns existed.
    # Previously closing a trade overwrote the `price` column with the exit price,
    # destroying the entry price. exit_price/pnl now hold that data instead.
    for ddl in (
        "ALTER TABLE orders ADD COLUMN exit_price REAL",
        "ALTER TABLE orders ADD COLUMN pnl REAL",
        "ALTER TABLE orders ADD COLUMN reasoning TEXT",
    ):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # Column already exists

    conn.commit()
    conn.close()

def init_db():
    _init_db_sync()
    print("Database tables initialized successfully.")

def _ensure_day_row(cursor, fallback_starting_capital: float):
    """
    Idempotent insert of today's (IST) daily_stats row if it doesn't already exist. Called at
    the top of every read/write in this module so a mid-session IST-midnight rollover on a
    24/7 deployment self-heals on the very next DB access, instead of requiring a restart.

    A brand-new day's starting capital COMPOUNDS forward from the most recent prior day's
    ending balance (starting_capital + realized_pnl) — a real trading account doesn't reset
    to a round number every morning, and an earlier version of this function did exactly
    that by design, which read as "my profit got wiped" the first time a real multi-day gap
    happened even though every trade record was still fully intact in the DB. Falls back to
    `fallback_starting_capital` (Config.STARTING_CAPITAL) only when there is no prior day at
    all — i.e. the very first row this account ever has, or immediately after a deliberate
    /admin/reset-simulation wipe (which deletes all daily_stats rows, so there's no "previous
    day" left to carry forward from).
    """
    today_str = _ist_today_str()
    cursor.execute("SELECT date FROM daily_stats WHERE date = ?", (today_str,))
    if cursor.fetchone():
        return  # today's row already exists — nothing to do

    cursor.execute(
        "SELECT starting_capital, realized_pnl FROM daily_stats WHERE date < ? ORDER BY date DESC LIMIT 1",
        (today_str,)
    )
    prior = cursor.fetchone()
    carried_capital = (prior[0] + prior[1]) if prior else fallback_starting_capital

    cursor.execute(
        "INSERT INTO daily_stats (date, starting_capital) VALUES (?, ?) ON CONFLICT(date) DO NOTHING",
        (today_str, carried_capital)
    )

def _start_day_stats_sync(starting_capital: float):
    """
    Called once at every container startup. Only seeds today's row if it doesn't already
    exist (via _ensure_day_row) — it must NOT force-overwrite starting_capital on every
    restart, or a same-day redeploy would silently reset a carried-forward or
    already-accumulating balance back to the fixed config value.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        _ensure_day_row(cursor, starting_capital)
        conn.commit()
    finally:
        conn.close()

def start_day_stats(starting_capital: float = None):
    from src.config import Config
    _start_day_stats_sync(starting_capital if starting_capital is not None else Config.STARTING_CAPITAL)

async def start_day_stats_async(starting_capital: float = None):
    from src.config import Config
    await asyncio.to_thread(_start_day_stats_sync, starting_capital if starting_capital is not None else Config.STARTING_CAPITAL)

def get_realized_pnl(cursor=None) -> float:
    from src.config import Config
    today_str = _ist_today_str()
    if cursor is not None:
        _ensure_day_row(cursor, Config.STARTING_CAPITAL)
        cursor.execute("SELECT realized_pnl FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        return row[0] if row else 0.0

    conn = get_connection()
    cursor_loc = conn.cursor()
    try:
        _ensure_day_row(cursor_loc, Config.STARTING_CAPITAL)
        conn.commit()
        cursor_loc.execute("SELECT realized_pnl FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor_loc.fetchone()
        return row[0] if row else 0.0
    finally:
        conn.close()

async def get_realized_pnl_async() -> float:
    return await asyncio.to_thread(get_realized_pnl)

def update_realized_pnl(amount: float, cursor=None):
    from src.config import Config
    today_str = _ist_today_str()
    if cursor is not None:
        # UPSERT, not a plain UPDATE: a plain UPDATE against a date with no existing row is
        # a silent no-op — that was the exact bug that lost every trade's realized P&L closed
        # after an IST-midnight rollover on a long-running deployment.
        _ensure_day_row(cursor, Config.STARTING_CAPITAL)
        cursor.execute(
            "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ?",
            (amount, today_str)
        )
        return

    conn = get_connection()
    cursor_loc = conn.cursor()
    try:
        _ensure_day_row(cursor_loc, Config.STARTING_CAPITAL)
        cursor_loc.execute(
            "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ?",
            (amount, today_str)
        )
        conn.commit()
    finally:
        conn.close()

def get_consecutive_losses() -> int:
    from src.config import Config
    today_str = _ist_today_str()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        _ensure_day_row(cursor, Config.STARTING_CAPITAL)
        conn.commit()
        cursor.execute("SELECT consecutive_losses FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()

def set_consecutive_losses(count: int, cursor=None):
    from src.config import Config
    today_str = _ist_today_str()
    if cursor is not None:
        _ensure_day_row(cursor, Config.STARTING_CAPITAL)
        cursor.execute("UPDATE daily_stats SET consecutive_losses = ? WHERE date = ?", (count, today_str))
        return

    conn = get_connection()
    cursor_loc = conn.cursor()
    try:
        _ensure_day_row(cursor_loc, Config.STARTING_CAPITAL)
        cursor_loc.execute("UPDATE daily_stats SET consecutive_losses = ? WHERE date = ?", (count, today_str))
        conn.commit()
    finally:
        conn.close()

def get_starting_capital() -> float:
    from src.config import Config
    today_str = _ist_today_str()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        _ensure_day_row(cursor, Config.STARTING_CAPITAL)
        conn.commit()
        cursor.execute("SELECT starting_capital FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        # Fall back to the configured value, not a hardcoded literal that has drifted out of
        # sync with the account's actual configured capital for a long time now.
        return row[0] if row else Config.STARTING_CAPITAL
    finally:
        conn.close()

def _reset_simulation_sync():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM orders")
        cursor.execute("DELETE FROM daily_stats")
        conn.commit()
        print("[DB] Simulation reset: all orders and daily stats wiped.")
    finally:
        conn.close()

def reset_simulation():
    _reset_simulation_sync()

async def execute_in_db(func, *args):
    """Utility to run a synchronous DB function containing its own connection lifecycle in a separate thread."""
    return await asyncio.to_thread(func, *args)

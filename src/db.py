import sqlite3
import os
import asyncio
from datetime import date

DATABASE_FILE = os.getenv("DATABASE_PATH", "trading.db")

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
            realized_pnl REAL DEFAULT 0.0
        )
    """)
    conn.commit()
    conn.close()

def init_db():
    _init_db_sync()
    print("Database tables initialized successfully.")

def _start_day_stats_sync(starting_capital: float):
    today_str = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO daily_stats (date, starting_capital) 
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET starting_capital = excluded.starting_capital
        """, (today_str, starting_capital))
        conn.commit()
    finally:
        conn.close()

def start_day_stats(starting_capital: float = 2000.0):
    _start_day_stats_sync(starting_capital)

async def start_day_stats_async(starting_capital: float = 2000.0):
    await asyncio.to_thread(_start_day_stats_sync, starting_capital)

def get_realized_pnl(cursor=None) -> float:
    today_str = date.today().isoformat()
    if cursor is not None:
        cursor.execute("SELECT realized_pnl FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        return row[0] if row else 0.0
        
    conn = get_connection()
    cursor_loc = conn.cursor()
    try:
        cursor_loc.execute("SELECT realized_pnl FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor_loc.fetchone()
        return row[0] if row else 0.0
    finally:
        conn.close()

async def get_realized_pnl_async() -> float:
    return await asyncio.to_thread(get_realized_pnl)

def update_realized_pnl(amount: float, cursor=None):
    today_str = date.today().isoformat()
    if cursor is not None:
        cursor.execute(
            "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ?",
            (amount, today_str)
        )
        return
        
    conn = get_connection()
    cursor_loc = conn.cursor()
    try:
        cursor_loc.execute(
            "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ?",
            (amount, today_str)
        )
        conn.commit()
    finally:
        conn.close()

def get_starting_capital() -> float:
    today_str = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT starting_capital FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        return row[0] if row else 2000.0
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

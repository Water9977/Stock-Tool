import sqlite3
import os
from datetime import date

DATABASE_FILE = os.getenv("DATABASE_PATH", "trading.db")

def get_connection():
    return sqlite3.connect(DATABASE_FILE)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Create orders table for logging virtual and live trades
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
    
    # 2. Create daily stats table for enforcing maximum daily loss rules
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            starting_capital REAL NOT NULL,
            realized_pnl REAL DEFAULT 0.0
        )
    """)
    
    conn.commit()
    conn.close()
    print("Database tables initialized successfully.")

def start_day_stats(starting_capital: float = 2000.0):
    """Initializes daily stats for today if not already present."""
    today_str = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO daily_stats (date, starting_capital) VALUES (?, ?)",
            (today_str, starting_capital)
        )
        conn.commit()
    finally:
        conn.close()

def get_realized_pnl() -> float:
    """Returns today's realized profit or loss."""
    today_str = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT realized_pnl FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        return row[0] if row else 0.0
    finally:
        conn.close()

def update_realized_pnl(amount: float):
    """Adds to today's realized profit or loss."""
    today_str = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE daily_stats SET realized_pnl = realized_pnl + ? WHERE date = ?",
            (amount, today_str)
        )
        conn.commit()
    finally:
        conn.close()

def get_starting_capital() -> float:
    """Returns today's starting capital."""
    today_str = date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT starting_capital FROM daily_stats WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        return row[0] if row else 2000.0
    finally:
        conn.close()

import os
from dotenv import load_dotenv

# Load variables from .env if it exists
load_dotenv()

class Config:
    CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
    CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
    REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")
    
    MOBILE_NUMBER = os.getenv("UPSTOX_MOBILE_NUMBER")
    PIN = os.getenv("UPSTOX_PIN")
    TOTP_SECRET = os.getenv("UPSTOX_TOTP_SECRET")
    STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", 2000.0))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 500.0))
    # Per-trade cap kept below total capital so Rs.2000 can hold 2-3 concurrent positions (diversification).
    MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", 700.0))

    TRADING_MODE = os.getenv("TRADING_MODE", "VIRTUAL").upper()

    # OHLC candle aggregation period (seconds). Live NSE uses 300 (5-min candles).
    # For VIRTUAL end-to-end testing this can be lowered so indicators populate fast
    # enough to actually generate trades within a short simulation window.
    CANDLE_PERIOD_SECONDS = int(os.getenv("CANDLE_PERIOD_SECONDS", 300))

    # VIRTUAL-mode auto-flatten + suspend timer (hours).
    VIRTUAL_RUNTIME_HOURS = float(os.getenv("VIRTUAL_RUNTIME_HOURS", 5.0))

    # Watchlist constrained to liquid NSE large-caps priced under Rs.600 so every symbol is
    # individually tradeable within a Rs.2000 account. ISINs verified against NSE/Upstox.
    SYMBOL_MAP = {
        "SBIN.NS":      "NSE_EQ|INE062A01020",  # ~Rs.580
        "ITC.NS":       "NSE_EQ|INE154A01025",  # ~Rs.290
        "NTPC.NS":      "NSE_EQ|INE733E01010",  # ~Rs.352
        "ONGC.NS":      "NSE_EQ|INE213A01029",  # ~Rs.233
        "COALINDIA.NS": "NSE_EQ|INE522F01014",  # ~Rs.450
    }

    @classmethod
    def validate(cls):
        """Validates that OAuth credentials are set when running in LIVE or PAPER mode."""
        if cls.TRADING_MODE in ["LIVE", "PAPER"]:
            missing = [k for k in ["CLIENT_ID", "CLIENT_SECRET"] if not getattr(cls, k)]
            if missing:
                raise ValueError(f"Missing required credentials in .env: {', '.join(missing)}")
        return True

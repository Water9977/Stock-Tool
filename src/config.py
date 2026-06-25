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
    MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", 2000.0))
    
    TRADING_MODE = os.getenv("TRADING_MODE", "VIRTUAL").upper()
    
    SYMBOL_MAP = {
        "RELIANCE.NS":  "NSE_EQ|INE002A01018",
        "TCS.NS":       "NSE_EQ|INE467B01029",
        "INFY.NS":      "NSE_EQ|INE009A01021",
        "SBIN.NS":      "NSE_EQ|INE062A01020",
        "AXISBANK.NS":  "NSE_EQ|INE238A01034",
    }

    @classmethod
    def validate(cls):
        """Validates that credentials are set when running in live mode."""
        if cls.TRADING_MODE in ["LIVE", "PAPER"]:
            missing = [k for k in ["CLIENT_ID", "CLIENT_SECRET", "MOBILE_NUMBER", "PIN", "TOTP_SECRET"] if not getattr(cls, k)]
            if missing:
                raise ValueError(f"Missing required live/paper trading credentials in .env: {', '.join(missing)}")
        return True

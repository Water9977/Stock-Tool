import os
from datetime import datetime, time
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

    # Shared secret required (via X-API-Key header) to call mutating endpoints (/execute,
    # /admin/reset-simulation). If unset, those endpoints refuse all requests rather than
    # silently allowing unauthenticated order placement / DB wipes on a public URL.
    API_KEY = os.getenv("API_KEY")

    # Upstox mandates order placement/modification/cancellation requests originate from a
    # registered static IP (error UDAPI1154 otherwise) — cloud hosts like Railway rotate
    # outbound IPs by default. Set this to an HTTP(S) proxy URL from a static-IP provider
    # (e.g. QuotaGuard Static, a fixed-IP VPS) and register that proxy's IP in the Upstox
    # developer console. Left unset, order calls go out on Railway's normal rotating IP,
    # which WILL be rejected in LIVE mode once real orders are attempted — this is provider
    # account setup this codebase cannot provision on its own; None here is a valid no-op
    # (httpx.AsyncClient(proxy=None) behaves as if no proxy were configured at all).
    UPSTOX_HTTP_PROXY = os.getenv("UPSTOX_HTTP_PROXY")
    STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", 300000.0))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 200000.0))
    # Per-trade cap kept low so many concurrent positions can run across different symbols at
    # once, instead of 1-2 trades locking up the whole account for hours waiting on a target.
    MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", 80000.0))

    # Target/stop-loss percentages for new entries. Small on purpose — the point is fast,
    # frequent, small-profit trades that free up capital quickly for the next signal, rather
    # than a big target that can take all day (or never) to hit while capital sits idle.
    TARGET_PCT = float(os.getenv("TARGET_PCT", 0.01))
    SL_PCT = float(os.getenv("SL_PCT", 0.005))

    TRADING_MODE = os.getenv("TRADING_MODE", "VIRTUAL").upper()

    # OHLC candle aggregation period (seconds). Live NSE uses 300 (5-min candles).
    # For VIRTUAL end-to-end testing this can be lowered so indicators populate fast
    # enough to actually generate trades within a short simulation window.
    CANDLE_PERIOD_SECONDS = int(os.getenv("CANDLE_PERIOD_SECONDS", 300))

    # VIRTUAL-mode auto-flatten + suspend timer (hours).
    VIRTUAL_RUNTIME_HOURS = float(os.getenv("VIRTUAL_RUNTIME_HOURS", 5.0))

    # Nifty50 constituents individually tradeable within Rs.3500 per-trade cap (price <= cap
    # at time of writing). Instrument keys verified against Upstox's public instrument master
    # (assets.upstox.com/market-quote/instruments/exchange/complete.json.gz), not guessed.
    # Excluded (real Nifty50 members, but too expensive for the cap): MARUTI, ULTRACEMCO,
    # BAJAJ-AUTO, APOLLOHOSP, EICHERMOT, DIVISLAB, BRITANNIA, HEROMOTOCO, TITAN, LT.
    # Note: TATAMOTORS demerged in 2025 into passenger-vehicle (TMPV) and commercial-vehicle
    # (TMCV) entities; TMPV is used here. LTIMINDTREE could not be located in the instrument
    # master under any known trading symbol and was dropped rather than guessed.
    SYMBOL_MAP = {
        "WIPRO.NS":       "NSE_EQ|INE075A01022",  # ~Rs.170
        "TATASTEEL.NS":   "NSE_EQ|INE081A01020",  # ~Rs.187
        "ONGC.NS":        "NSE_EQ|INE213A01029",  # ~Rs.235
        "POWERGRID.NS":   "NSE_EQ|INE752E01010",  # ~Rs.287
        "ITC.NS":         "NSE_EQ|INE154A01025",  # ~Rs.290
        "BPCL.NS":        "NSE_EQ|INE029A01011",  # ~Rs.303
        "TATAMOTORS.NS":  "NSE_EQ|INE155A01022",  # ~Rs.349 (Tata Motors Passenger Vehicles)
        "NTPC.NS":        "NSE_EQ|INE733E01010",  # ~Rs.359
        "KOTAKBANK.NS":   "NSE_EQ|INE237A01036",  # ~Rs.397
        "COALINDIA.NS":   "NSE_EQ|INE522F01014",  # ~Rs.436
        "UPL.NS":         "NSE_EQ|INE628A01036",  # ~Rs.568
        "HDFCLIFE.NS":    "NSE_EQ|INE795G01014",  # ~Rs.569
        "HDFCBANK.NS":    "NSE_EQ|INE040A01034",  # ~Rs.801
        "HINDALCO.NS":    "NSE_EQ|INE038A01020",  # ~Rs.941
        "INDUSINDBK.NS":  "NSE_EQ|INE095A01012",  # ~Rs.944
        "INFY.NS":        "NSE_EQ|INE009A01021",  # ~Rs.997
        "BAJFINANCE.NS":  "NSE_EQ|INE296A01032",  # ~Rs.1023
        "SBIN.NS":        "NSE_EQ|INE062A01020",  # ~Rs.1038
        "SHRIRAMFIN.NS":  "NSE_EQ|INE721A01047",  # ~Rs.1051
        "HCLTECH.NS":     "NSE_EQ|INE860A01027",  # ~Rs.1055
        "TATACONSUM.NS":  "NSE_EQ|INE192A01025",  # ~Rs.1084
        "JSWSTEEL.NS":    "NSE_EQ|INE019A01038",  # ~Rs.1216
        "RELIANCE.NS":    "NSE_EQ|INE002A01018",  # ~Rs.1310
        "DRREDDY.NS":     "NSE_EQ|INE089A01031",  # ~Rs.1337
        "TECHM.NS":       "NSE_EQ|INE669C01036",  # ~Rs.1362
        "AXISBANK.NS":    "NSE_EQ|INE238A01034",  # ~Rs.1375
        "ICICIBANK.NS":   "NSE_EQ|INE090A01021",  # ~Rs.1382
        "NESTLEIND.NS":   "NSE_EQ|INE239A01024",  # ~Rs.1443
        "CIPLA.NS":       "NSE_EQ|INE059A01026",  # ~Rs.1448
        "SBILIFE.NS":     "NSE_EQ|INE123W01016",  # ~Rs.1773
        "BAJAJFINSV.NS":  "NSE_EQ|INE918I01026",  # ~Rs.1780
        "ADANIPORTS.NS":  "NSE_EQ|INE742F01042",  # ~Rs.1859
        "SUNPHARMA.NS":   "NSE_EQ|INE044A01036",  # ~Rs.1863
        "BHARTIARTL.NS":  "NSE_EQ|INE397D01024",  # ~Rs.1875
        "TCS.NS":         "NSE_EQ|INE467B01029",  # ~Rs.2033
        "HINDUNILVR.NS":  "NSE_EQ|INE030A01027",  # ~Rs.2164
        "ASIANPAINT.NS":  "NSE_EQ|INE021A01026",  # ~Rs.2711
        "ADANIENT.NS":    "NSE_EQ|INE423A01024",  # ~Rs.3106
        "M&M.NS":         "NSE_EQ|INE101A01026",  # ~Rs.3134
        "GRASIM.NS":      "NSE_EQ|INE047A01021",  # ~Rs.3143
    }

    @classmethod
    def validate(cls):
        """Validates that OAuth credentials are set when running in LIVE or PAPER mode."""
        if cls.TRADING_MODE in ["LIVE", "PAPER"]:
            missing = [k for k in ["CLIENT_ID", "CLIENT_SECRET"] if not getattr(cls, k)]
            if missing:
                raise ValueError(f"Missing required credentials in .env: {', '.join(missing)}")
        return True

    @classmethod
    def is_market_hours(cls) -> bool:
        """
        True if current IST time is within NSE trading hours (09:15-15:20) on a weekday.
        VIRTUAL mode always returns True so the demo simulator can run around the clock.
        """
        if cls.TRADING_MODE == "VIRTUAL":
            return True

        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist)

        if now.weekday() >= 5:
            return False

        market_start = time(9, 15)
        market_end = time(15, 20)
        return market_start <= now.time() <= market_end

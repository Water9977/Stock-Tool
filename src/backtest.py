"""
Backtesting Engine (Phase 2)
============================
Feeds historical OHLCV data from yfinance into the TechnicalEngine tick-by-tick
to simulate how the strategy would have performed historically.

Usage:
    python src/backtest.py --symbol RELIANCE.NS --interval 5m --period 5d
    python src/backtest.py --symbol TCS.NS --interval 1d --period 6mo
"""
import asyncio
import sys
import os
import argparse
from datetime import datetime

# Add project root to path for absolute imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

from src.technicals import TechnicalEngine


# Mapping from Yahoo Finance ticker to Upstox instrument key
SYMBOL_MAP = {
    "RELIANCE.NS":  "NSE_EQ|INE002A01018",
    "TCS.NS":       "NSE_EQ|INE467B01029",
    "INFY.NS":      "NSE_EQ|INE009A01021",
    "SBIN.NS":      "NSE_EQ|INE062A01020",
    "AXISBANK.NS":  "NSE_EQ|INE238A01034",
}


class BacktestResult:
    """Accumulates P&L, trade log, and summary stats from a backtest run."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.trades = []           # list of dicts: {entry, exit, side, pnl}
        self.open_trade = None     # currently open position
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0

    def open_position(self, price: float, side: str, timestamp):
        if self.open_trade is not None:
            return  # One position at a time
        self.open_trade = {"entry_price": price, "side": side, "entry_time": timestamp}

    def close_position(self, price: float, timestamp, reason: str):
        if self.open_trade is None:
            return
        entry = self.open_trade["entry_price"]
        side = self.open_trade["side"]

        if side == "BUY":
            pnl = round((price - entry) * 10, 2)   # 10 qty
        else:
            pnl = round((entry - price) * 10, 2)

        self.total_pnl += pnl
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        self.trades.append({
            "entry_time": self.open_trade["entry_time"],
            "exit_time": timestamp,
            "side": side,
            "entry_price": entry,
            "exit_price": price,
            "pnl": pnl,
            "reason": reason,
        })
        self.open_trade = None

    def print_summary(self):
        total_trades = self.wins + self.losses
        win_rate = (self.wins / total_trades * 100) if total_trades else 0
        print("\n" + "=" * 60)
        print(f"  BACKTEST RESULTS — {self.symbol}")
        print("=" * 60)
        print(f"  Total Trades  : {total_trades}")
        print(f"  Wins          : {self.wins}  |  Losses: {self.losses}")
        print(f"  Win Rate      : {win_rate:.1f}%")
        print(f"  Total P&L     : Rs.{self.total_pnl:+.2f}")
        if self.trades:
            max_drawdown = min((t["pnl"] for t in self.trades), default=0.0)
            print(f"  Max Drawdown  : Rs.{max_drawdown:.2f}")
        print("=" * 60)
        if self.trades:
            print("\n  Trade Log (last 10):")
            for t in self.trades[-10:]:
                pnl_str = f"Rs.{t['pnl']:+.2f}"
                print(
                    f"  [{t['entry_time']}] {t['side']:4s}  "
                    f"In: {t['entry_price']:.2f}  Out: {t['exit_price']:.2f}  "
                    f"P&L: {pnl_str:>12s}  ({t['reason']})"
                )


async def run_backtest(yf_symbol: str = "RELIANCE.NS", interval: str = "5m", period: str = "5d"):
    """
    Downloads historical data from Yahoo Finance and replays it tick-by-tick
    through the TechnicalEngine to generate backtest signals.

    Target/Stop are applied as 2% / 1% of entry price (same as live).
    """
    upstox_symbol = SYMBOL_MAP.get(yf_symbol, f"NSE_EQ|{yf_symbol}")

    print(f"\nBacktest starting: {yf_symbol} | Interval: {interval} | Period: {period}")
    print("Downloading historical data from Yahoo Finance...")

    try:
        df = await asyncio.to_thread(
            lambda: yf.download(yf_symbol, interval=interval, period=period, progress=False, auto_adjust=True)
        )
    except Exception as e:
        print(f"ERROR: Failed to download data from Yahoo Finance: {e}")
        return

    if df.empty:
        print("ERROR: No data returned. Try a different period or interval.")
        return

    # Flatten multi-level columns if present (yfinance sometimes returns them)
    if isinstance(df.columns, object) and hasattr(df.columns, 'levels'):
        df.columns = df.columns.droplevel(1)

    print(f"Downloaded {len(df)} candles. Starting replay...\n")

    technicals = TechnicalEngine()
    tick_queue = asyncio.Queue()
    result = BacktestResult(yf_symbol)

    # Start warm path indicator engine
    engine_task = asyncio.create_task(technicals.run(tick_queue))

    TARGET_PCT = 0.02   # 2% target
    SL_PCT = 0.01       # 1% stop loss

    for ts, row in df.iterrows():
        close_price = float(row["Close"])
        timestamp = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, 'strftime') else str(ts)

        # Feed tick into the Warm Path
        await tick_queue.put({"symbol": upstox_symbol, "price": close_price})
        await asyncio.sleep(0)   # yield to let indicator engine process

        signal = technicals.get_signal(upstox_symbol)

        # Check open position first (exit conditions)
        if result.open_trade is not None:
            entry = result.open_trade["entry_price"]
            side = result.open_trade["side"]

            if side == "BUY":
                target = entry * (1 + TARGET_PCT)
                sl = entry * (1 - SL_PCT)
                if close_price >= target:
                    result.close_position(close_price, timestamp, "Target Hit")
                elif close_price <= sl:
                    result.close_position(close_price, timestamp, "Stop Loss")
            else:  # SELL (short)
                target = entry * (1 - TARGET_PCT)
                sl = entry * (1 + SL_PCT)
                if close_price <= target:
                    result.close_position(close_price, timestamp, "Target Hit")
                elif close_price >= sl:
                    result.close_position(close_price, timestamp, "Stop Loss")

        # Enter new position if no open trade and signal exists
        if result.open_trade is None and signal == "BUY":
            result.open_position(close_price, "BUY", timestamp)
            rsi_val = technicals.indicators[upstox_symbol]['rsi'][-1] if upstox_symbol in technicals.indicators else 0
            print(f"  [ENTER BUY ] @ Rs.{close_price:.2f} | RSI: {rsi_val:.1f} | {timestamp}")

        elif result.open_trade is None and signal == "SELL":
            result.open_position(close_price, "SELL", timestamp)
            rsi_val = technicals.indicators[upstox_symbol]['rsi'][-1] if upstox_symbol in technicals.indicators else 0
            print(f"  [ENTER SELL] @ Rs.{close_price:.2f} | RSI: {rsi_val:.1f} | {timestamp}")

    # Force-close any remaining position at last price
    if result.open_trade is not None:
        last_price = float(df["Close"].iloc[-1])
        result.close_position(last_price, "end-of-data", "EOD Force Close")
        print(f"  [FORCE CLOSE] @ Rs.{last_price:.2f}")

    engine_task.cancel()
    result.print_summary()


def main():
    parser = argparse.ArgumentParser(description="Stock Tool — Historical Backtester")
    parser.add_argument("--symbol", default="RELIANCE.NS", help="Yahoo Finance ticker (default: RELIANCE.NS)")
    parser.add_argument("--interval", default="5m", help="Bar interval: 1m, 5m, 15m, 1h, 1d (default: 5m)")
    parser.add_argument("--period", default="5d", help="History period: 1d, 5d, 1mo, 3mo, 6mo, 1y (default: 5d)")
    args = parser.parse_args()

    asyncio.run(run_backtest(args.symbol, args.interval, args.period))


if __name__ == "__main__":
    main()

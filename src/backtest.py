"""
Backtesting Engine (Phase 2)
============================
Feeds historical OHLCV data from yfinance into the TechnicalEngine tick-by-tick
to simulate how the strategy would have performed historically.

Usage:
    python src/backtest.py --symbol SBIN.NS --interval 5m --period 5d
    python src/backtest.py --symbol ITC.NS --interval 1d --period 6mo
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
from src.config import Config

# Mapping from Yahoo Finance ticker to Upstox instrument key — single source of truth,
# shared with the live watchlist so this module can't drift out of sync with it.
SYMBOL_MAP = Config.SYMBOL_MAP


class BacktestResult:
    """Accumulates P&L, transaction costs, trade log, and net stats from a backtest run."""

    def __init__(self, symbol: str, qty: int = 10, slippage_pct: float = 0.0005):
        self.symbol = symbol
        self.qty = qty
        self.slippage_pct = slippage_pct
        self.trades = []           # list of dicts: {entry_time, exit_time, side, entry_price, exit_price, gross_pnl, charges, net_pnl, reason}
        self.open_trade = None     # currently open position
        self.total_gross_pnl = 0.0
        self.total_charges = 0.0
        self.total_net_pnl = 0.0
        self.wins = 0
        self.losses = 0

    def open_position(self, price: float, side: str, timestamp):
        if self.open_trade is not None:
            return  # One position at a time
        
        # Apply entry slippage penalty
        if side == "BUY":
            executed_price = round(price * (1 + self.slippage_pct), 2)
        else: # SELL
            executed_price = round(price * (1 - self.slippage_pct), 2)
            
        self.open_trade = {
            "raw_entry_price": price,
            "entry_price": executed_price,
            "side": side,
            "entry_time": timestamp
        }

    def _calculate_charges(self, buy_price: float, sell_price: float) -> float:
        """
        Calculates standard Upstox intraday equity transaction charges and govt taxes.
        - Brokerage: 0.05% of turnover or Rs. 20 (whichever is lower) per execution.
        - STT (Securities Transaction Tax): 0.025% on sell side.
        - Exchange Transaction Charge (NSE): 0.00322% of total turnover.
        - GST: 18% of (Brokerage + Exchange Charges).
        - SEBI Turnover Fee: 0.0001% of total turnover.
        - Stamp Duty: 0.003% on buy side.
        """
        buy_value = buy_price * self.qty
        sell_value = sell_price * self.qty
        turnover = buy_value + sell_value
        
        # 1. Brokerage
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

    def close_position(self, price: float, timestamp, reason: str):
        if self.open_trade is None:
            return
        
        entry = self.open_trade["entry_price"]
        side = self.open_trade["side"]

        # Apply exit slippage penalty
        if side == "BUY":
            executed_exit_price = round(price * (1 - self.slippage_pct), 2)
            buy_price, sell_price = entry, executed_exit_price
            gross_pnl = round((executed_exit_price - entry) * self.qty, 2)
        else: # SELL (short)
            executed_exit_price = round(price * (1 + self.slippage_pct), 2)
            buy_price, sell_price = executed_exit_price, entry
            gross_pnl = round((entry - executed_exit_price) * self.qty, 2)

        charges = self._calculate_charges(buy_price, sell_price)
        net_pnl = round(gross_pnl - charges, 2)

        self.total_gross_pnl += gross_pnl
        self.total_charges += charges
        self.total_net_pnl += net_pnl

        if net_pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        self.trades.append({
            "entry_time": self.open_trade["entry_time"],
            "exit_time": timestamp,
            "side": side,
            "entry_price": entry,
            "exit_price": executed_exit_price,
            "gross_pnl": gross_pnl,
            "charges": charges,
            "net_pnl": net_pnl,
            "pnl": net_pnl,   # For UI compatibility
            "reason": reason,
        })
        self.open_trade = None

    def print_summary(self):
        total_trades = self.wins + self.losses
        win_rate = (self.wins / total_trades * 100) if total_trades else 0
        print("\n" + "=" * 60)
        print(f"  BACKTEST RESULTS — {self.symbol} (Slippage: {self.slippage_pct*100:.3f}%)")
        print("=" * 60)
        print(f"  Total Trades  : {total_trades}")
        print(f"  Wins          : {self.wins}  |  Losses: {self.losses}")
        print(f"  Win Rate      : {win_rate:.1f}%")
        print(f"  Gross P&L     : Rs.{self.total_gross_pnl:+.2f}")
        print(f"  Total Charges : Rs.{self.total_charges:.2f} (STT/Stamp/GST/Brokerage)")
        print(f"  Net P&L       : Rs.{self.total_net_pnl:+.2f}")
        if self.trades:
            max_drawdown = min((t["net_pnl"] for t in self.trades), default=0.0)
            print(f"  Max Drawdown  : Rs.{max_drawdown:.2f}")
        print("=" * 60)
        if self.trades:
            print("\n  Trade Log (last 10):")
            for t in self.trades[-10:]:
                print(
                    f"  [{t['entry_time']}] {t['side']:4s}  "
                    f"In: {t['entry_price']:.2f}  Out: {t['exit_price']:.2f}  "
                    f"Gross P&L: Rs.{t['gross_pnl']:+.2f}  Charges: Rs.{t['charges']:.2f}  "
                    f"Net P&L: Rs.{t['net_pnl']:+.2f}  ({t['reason']})"
                )


async def run_backtest(yf_symbol: str = "SBIN.NS", interval: str = "5m", period: str = "5d"):
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
    parser.add_argument("--symbol", default="SBIN.NS", help="Yahoo Finance ticker (default: RELIANCE.NS)")
    parser.add_argument("--interval", default="5m", help="Bar interval: 1m, 5m, 15m, 1h, 1d (default: 5m)")
    parser.add_argument("--period", default="5d", help="History period: 1d, 5d, 1mo, 3mo, 6mo, 1y (default: 5d)")
    args = parser.parse_args()

    asyncio.run(run_backtest(args.symbol, args.interval, args.period))


if __name__ == "__main__":
    main()

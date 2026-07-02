import asyncio
from talipp.indicators import RSI, MACD

class TechnicalEngine:
    def __init__(self):
        # We track separate technicals for each symbol
        self.indicators = {}
        self.signals = {} # Latest technical signals (e.g. BUY / SELL)

    def _init_symbol(self, symbol: str):
        if symbol not in self.indicators:
            # Standard Intraday Setup: RSI(14) and MACD(12, 26, 9)
            self.indicators[symbol] = {
                "rsi": RSI(14),
                "macd": MACD(12, 26, 9)
            }
            self.signals[symbol] = "NEUTRAL"

    async def run(self, tick_queue: asyncio.Queue):
        """
        The WARM PATH (10-100ms)
        Consumes ticks from the queue and calculates live indicators.
        """
        print("Starting Warm Path indicator engine...")
        while True:
            tick = await tick_queue.get()
            symbol = tick["symbol"]
            price = tick["price"]
            
            self._init_symbol(symbol)
            inds = self.indicators[symbol]
            
            # Feed the new price into the indicators
            inds["rsi"].add(price)
            inds["macd"].add(price)
            
            # Generate signals only if we have enough data (talipp lists are populated)
            if len(inds["rsi"]) >= 14 and len(inds["macd"]) >= 26:
                current_rsi = inds["rsi"][-1]
                current_macd = inds["macd"][-1]
                macd_line = current_macd.macd if current_macd else None
                macd_signal = current_macd.signal if current_macd else None
                macd_bullish = macd_line is not None and macd_signal is not None and macd_line > macd_signal
                macd_bearish = macd_line is not None and macd_signal is not None and macd_line < macd_signal

                # Either condition alone is enough to trigger — requiring both RSI extreme AND
                # MACD confirmation simultaneously was so rare across a trading day it produced
                # almost no signals at all. RSI<40/>60 with a same-direction MACD tiebreak on the
                # non-extreme side keeps this from firing on pure noise.
                if current_rsi < 40.0 or (macd_bullish and current_rsi < 50.0):
                    self.signals[symbol] = "BUY"
                elif current_rsi > 60.0 or (macd_bearish and current_rsi > 50.0):
                    self.signals[symbol] = "SELL"
                else:
                    self.signals[symbol] = "NEUTRAL"
            
            tick_queue.task_done()

    def get_signal(self, symbol: str) -> str:
        return self.signals.get(symbol, "NEUTRAL")

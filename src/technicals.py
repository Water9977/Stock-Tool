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
                # Simple condition: RSI < 35 (Oversold) -> Buy Signal
                # RSI > 65 (Overbought) -> Sell Signal
                if current_rsi < 35.0:
                    self.signals[symbol] = "BUY"
                elif current_rsi > 65.0:
                    self.signals[symbol] = "SELL"
                else:
                    self.signals[symbol] = "NEUTRAL"
            
            tick_queue.task_done()

    def get_signal(self, symbol: str) -> str:
        return self.signals.get(symbol, "NEUTRAL")

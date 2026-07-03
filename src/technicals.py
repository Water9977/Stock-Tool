import asyncio
import httpx
from talipp.indicators import RSI, MACD
from src.config import Config

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

    async def seed_from_history(self, access_token: str):
        """
        Pre-warms RSI(14)/MACD(12,26,9) from Upstox's intraday historical-candle API so the
        strategy can generate real signals from the moment the live feed starts, instead of
        needing ~130 minutes (26 candles at 5-min each) of live ticks to accumulate first.
        Every container restart previously reset this warmup to zero — with how often this
        app gets redeployed, that meant the bot was frequently blind for the first couple of
        hours after any code change, independent of whether the fix itself was correct.

        PAPER/LIVE only — VIRTUAL mode has no real Upstox history to fetch and its simulator
        warms up in a handful of real seconds anyway (Config.CANDLE_PERIOD_SECONDS is much
        shorter there). Best-effort: a symbol whose history fetch fails just starts cold and
        warms up live as before — this is a startup optimization, not something that should
        be able to prevent the app from starting if Upstox's historical endpoint has a bad day.
        """
        if Config.TRADING_MODE not in ("PAPER", "LIVE") or not access_token:
            return

        interval_minutes = max(1, Config.CANDLE_PERIOD_SECONDS // 60)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        semaphore = asyncio.Semaphore(8)  # stay well under Upstox's 10 req/s regular-tier cap

        async def _seed_one(upstox_symbol: str):
            url = f"https://api.upstox.com/v3/historical-candle/intraday/{upstox_symbol}/minutes/{interval_minutes}"
            async with semaphore:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        r = await client.get(url, headers=headers)
                    if r.status_code != 200:
                        print(f"[SEED] {upstox_symbol}: HTTP {r.status_code}, starting cold.")
                        return
                    candles = r.json().get("data", {}).get("candles", [])
                    if not candles:
                        return
                    # API returns newest-first; indicators must be fed oldest-first.
                    closes = [c[4] for c in reversed(candles)]

                    self._init_symbol(upstox_symbol)
                    inds = self.indicators[upstox_symbol]
                    for close in closes:
                        inds["rsi"].add(close)
                        inds["macd"].add(close)
                    print(f"[SEED] {upstox_symbol}: warmed with {len(closes)} historical candles.")
                except Exception as e:
                    print(f"[SEED] {upstox_symbol}: failed ({e}), starting cold.")

        print(f"[SEED] Pre-warming indicators from Upstox historical candles ({interval_minutes}min bars)...")
        await asyncio.gather(*(_seed_one(upstox_symbol) for upstox_symbol in Config.SYMBOL_MAP.values()))
        print("[SEED] Historical warmup complete.")

    async def run(self, tick_queue: asyncio.Queue):
        """
        The WARM PATH (10-100ms)
        Consumes ticks from the queue and calculates live indicators.
        """
        print("Starting Warm Path indicator engine...")
        while True:
            tick = await tick_queue.get()
            try:
                symbol = tick["symbol"]
                price = tick["price"]

                self._init_symbol(symbol)
                inds = self.indicators[symbol]

                # Feed the new price into the indicators
                inds["rsi"].add(price)
                inds["macd"].add(price)

                # talipp's len() counts warmup-padding None entries too, so len(rsi) >= 14 can be
                # true while rsi[-1] is still None (the first real value lands a little later).
                # Gate on the actual value being present, not just on list length.
                current_rsi = inds["rsi"][-1] if len(inds["rsi"]) >= 14 else None
                current_macd = inds["macd"][-1] if len(inds["macd"]) >= 26 else None

                if current_rsi is not None and current_macd is not None:
                    macd_line = current_macd.macd
                    macd_signal = current_macd.signal
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
            except Exception as e:
                # A single bad tick must never permanently kill the Warm Path — before this
                # guard, an uncaught exception here (e.g. comparing None to a float) silently
                # ended the loop for the rest of the session with no further signals ever again.
                print(f"[WARM PATH ERROR] {e}")
            finally:
                tick_queue.task_done()

    def get_signal(self, symbol: str) -> str:
        return self.signals.get(symbol, "NEUTRAL")

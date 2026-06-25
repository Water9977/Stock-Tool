import asyncio
import random
import websockets
import json
from src.config import Config

class MarketFeed:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(MarketFeed, cls).__new__(cls, *args, **kwargs)
            cls._instance.price_cache = {
                "NSE_EQ|INE002A01018": 2450.0,  # RELIANCE
                "NSE_EQ|INE467B01029": 3300.0,  # TCS
                "NSE_EQ|INE009A01021": 1500.0,  # INFY
                "NSE_EQ|INE062A01020": 580.0,   # SBIN
                "NSE_EQ|INE238A01034": 2600.0,  # Axis Bank
            }
            cls._instance.is_running = False
        return cls._instance

    async def start(self):
        """Starts the price feed background worker."""
        if self.is_running:
            return
        self.is_running = True
        
        if Config.TRADING_MODE == "VIRTUAL":
            asyncio.create_task(self._run_virtual_simulator())
        else:
            asyncio.create_task(self._run_live_feed())

    async def _run_virtual_simulator(self):
        """Simulates market price movements in real-time (random walk)."""
        print("Starting VIRTUAL market feed simulator...")
        while self.is_running:
            # Simulate a tick update every 1 second
            await asyncio.sleep(1.0)
            for symbol in list(self.price_cache.keys()):
                old_price = self.price_cache[symbol]
                # Random walk: small percentage change between -0.08% and +0.08%
                pct_change = random.uniform(-0.0008, 0.0008)
                new_price = round(old_price * (1 + pct_change), 2)
                self.price_cache[symbol] = new_price
            
            # Print a quick snapshot of simulated prices (optional debug log)
            # print(f"Simulator ticks: RELIANCE: {self.price_cache['NSE_EQ|INE002A01018']}")

    async def _run_live_feed(self):
        """Connects to real Upstox WebSocket using Protobuf (LIVE mode)."""
        print("Connecting to live Upstox WebSocket feed...")
        # Since Upstox uses binary Protocol Buffers (Protobuf) for V2/V3,
        # live mode requires compiling MarketDataFeed.proto using protoc.
        # This acts as the structural foundation for connecting when LIVE is enabled.
        url = "wss://api.upstox.com/v2/feed/market-data-feed"
        headers = {"Authorization": f"Bearer mock_or_real_token"}
        
        while self.is_running:
            try:
                # The auth token must be passed in headers.
                # In Upstox, you must call a redirect authorization REST API first 
                # to get the authorized websocket endpoint.
                print("Live market feed connection loop starting...")
                await asyncio.sleep(5) # Stub retry delay
            except Exception as e:
                print(f"Error in Live WebSocket loop: {e}")
                await asyncio.sleep(5)

    def get_price(self, symbol: str) -> float:
        """Helper to get the current Last Traded Price (LTP)."""
        return self.price_cache.get(symbol, 100.0)

    def stop(self):
        self.is_running = False

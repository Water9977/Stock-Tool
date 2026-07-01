"""
Market Feed (Phase 2)
=====================
Handles both VIRTUAL (random walk simulator) and LIVE (Upstox V3 WebSocket + Protobuf) modes.

LIVE MODE SETUP:
  1. The upstox-python-sdk ships with MarketDataFeedV3_pb2.py inside the package.
     We import it dynamically at runtime so we don't need to run protoc manually.
  2. Authorization flow:
     GET /feed/market-data-feed/authorize  -> returns authorizedRedirectUri (wss://)
     CONNECT wss://...                     -> binary protobuf stream
     SEND JSON subscription message        -> ticks start arriving
"""
import asyncio
import json
import random
import ssl
import httpx
import websockets
from src.config import Config


# ─────────────────────────────────────────────────────────────────────────────
#  Instrument keys (Upstox format) — derived from Config.SYMBOL_MAP so this module
#  can never drift out of sync with the watchlist (a prior hardcoded copy here caused
#  a stale-symbol bug where the frontend kept showing a delisted-from-watchlist stock).
# ─────────────────────────────────────────────────────────────────────────────
INSTRUMENT_KEYS = list(Config.SYMBOL_MAP.values())

# Snapshot LTPs (Jun 2026) used only as the seed value until the first real REST/WS tick
# arrives, and as the starting point for the VIRTUAL random-walk simulator. Any instrument
# key not present here (e.g. a symbol added to Config.SYMBOL_MAP later) falls back to 200.0.
_BASE_PRICE_SNAPSHOT = {
    "NSE_EQ|INE075A01022": 170.41,   # WIPRO
    "NSE_EQ|INE081A01020": 187.09,   # TATASTEEL
    "NSE_EQ|INE213A01029": 235.55,   # ONGC
    "NSE_EQ|INE752E01010": 287.25,   # POWERGRID
    "NSE_EQ|INE154A01025": 290.10,   # ITC
    "NSE_EQ|INE029A01011": 303.20,   # BPCL
    "NSE_EQ|INE155A01022": 349.10,   # TATAMOTORS
    "NSE_EQ|INE733E01010": 359.00,   # NTPC
    "NSE_EQ|INE237A01036": 396.85,   # KOTAKBANK
    "NSE_EQ|INE522F01014": 435.65,   # COALINDIA
    "NSE_EQ|INE628A01036": 567.85,   # UPL
    "NSE_EQ|INE795G01014": 568.50,   # HDFCLIFE
    "NSE_EQ|INE040A01034": 800.50,   # HDFCBANK
    "NSE_EQ|INE038A01020": 941.05,   # HINDALCO
    "NSE_EQ|INE095A01012": 943.95,   # INDUSINDBK
    "NSE_EQ|INE009A01021": 996.70,   # INFY
    "NSE_EQ|INE296A01032": 1023.00,  # BAJFINANCE
    "NSE_EQ|INE062A01020": 1037.70,  # SBIN
    "NSE_EQ|INE721A01047": 1050.70,  # SHRIRAMFIN
    "NSE_EQ|INE860A01027": 1054.50,  # HCLTECH
    "NSE_EQ|INE192A01025": 1084.00,  # TATACONSUM
    "NSE_EQ|INE019A01038": 1216.20,  # JSWSTEEL
    "NSE_EQ|INE002A01018": 1309.50,  # RELIANCE
    "NSE_EQ|INE089A01031": 1337.20,  # DRREDDY
    "NSE_EQ|INE669C01036": 1362.10,  # TECHM
    "NSE_EQ|INE238A01034": 1374.50,  # AXISBANK
    "NSE_EQ|INE090A01021": 1381.80,  # ICICIBANK
    "NSE_EQ|INE239A01024": 1443.40,  # NESTLEIND
    "NSE_EQ|INE059A01026": 1447.90,  # CIPLA
    "NSE_EQ|INE123W01016": 1772.70,  # SBILIFE
    "NSE_EQ|INE918I01026": 1779.80,  # BAJAJFINSV
    "NSE_EQ|INE742F01042": 1859.00,  # ADANIPORTS
    "NSE_EQ|INE044A01036": 1863.00,  # SUNPHARMA
    "NSE_EQ|INE397D01024": 1874.60,  # BHARTIARTL
    "NSE_EQ|INE467B01029": 2032.90,  # TCS
    "NSE_EQ|INE030A01027": 2164.00,  # HINDUNILVR
    "NSE_EQ|INE021A01026": 2711.20,  # ASIANPAINT
    "NSE_EQ|INE423A01024": 3106.00,  # ADANIENT
    "NSE_EQ|INE101A01026": 3133.50,  # M&M
    "NSE_EQ|INE047A01021": 3142.60,  # GRASIM
}
_BASE_PRICES = {key: _BASE_PRICE_SNAPSHOT.get(key, 200.0) for key in INSTRUMENT_KEYS}


def _load_proto_module():
    """
    Imports the Upstox Protobuf V3 module bundled inside upstox-python-sdk.
    Confirmed path: upstox_client.feeder.proto.MarketDataFeedV3_pb2

    FeedResponse fields : type, feeds (map<str,Feed>), currentTs, marketInfo
    Feed fields         : ltpc (LTPC), fullFeed (FullFeed), requestMode
    LTPC fields         : ltp, ltt, ltq, cp
    FullFeed fields     : marketFF (MarketFullFeed), indexFF
    MarketFullFeed      : ltpc, marketLevel, marketOHLC, atp, vtt, oi, ...
    """
    try:
        from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as pb2
        return pb2
    except ImportError:
        pass

    return None


class MarketFeed:
    """
    Singleton price-feed broker for the trading engine.

    Modes:
        VIRTUAL — Random-walk price simulator (no credentials needed)
        LIVE    — Real Upstox V3 WebSocket feed via Protobuf binary decoding
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(MarketFeed, cls).__new__(cls, *args, **kwargs)
            cls._instance.price_cache = dict(_BASE_PRICES)   # LTP cache
            cls._instance.is_running = False
            cls._instance.tick_queues = []
            cls._instance._access_token = None
            cls._instance.last_tick_time = None
            cls._instance.candles = {}  # symbol -> {period, open, high, low, close}
        return cls._instance

    def set_access_token(self, token: str):
        """Called after authentication to provide the bearer token for LIVE mode."""
        self._access_token = token

    def subscribe(self) -> asyncio.Queue:
        """Returns a bounded queue for consuming ticks (used by Warm Path)."""
        q = asyncio.Queue(maxsize=200)
        self.tick_queues.append(q)
        return q

    async def _process_tick(self, symbol: str, price: float):
        """Aggregates 1-second ticks into 5-minute OHLC candles and broadcasts the closed candle."""
        self.price_cache[symbol] = price
        
        import time
        current_period = int(time.time() // Config.CANDLE_PERIOD_SECONDS)
        
        if symbol not in self.candles:
            self.candles[symbol] = {'period': current_period, 'open': price, 'high': price, 'low': price, 'close': price}
        else:
            candle = self.candles[symbol]
            if current_period > candle['period']:
                # Candle closed, broadcast the close price
                await self._broadcast_tick(symbol, candle['close'])
                # Start new candle
                self.candles[symbol] = {'period': current_period, 'open': price, 'high': price, 'low': price, 'close': price}
            else:
                # Update current candle
                candle['high'] = max(candle['high'], price)
                candle['low'] = min(candle['low'], price)
                candle['close'] = price

    async def _broadcast_tick(self, symbol: str, price: float):
        """Sends a tick to all subscribers. Drops tick silently if a subscriber queue is full."""
        tick = {"symbol": symbol, "price": price}
        for q in self.tick_queues:
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:
                pass   # Tick shedding – never block the feed

    async def start(self):
        """Starts the price feed background worker."""
        if self.is_running:
            return
        self.is_running = True

        if Config.TRADING_MODE == "VIRTUAL":
            asyncio.create_task(self._run_virtual_simulator())
        else:
            asyncio.create_task(self._run_live_feed())

    # ─────────────────────────────────────────────────────────────────────────
    #  VIRTUAL MODE
    # ─────────────────────────────────────────────────────────────────────────
    async def _run_virtual_simulator(self):
        """Simulates realistic intraday price movements (random walk with drift)."""
        print("[FEED] Starting VIRTUAL market feed simulator...")
        while self.is_running:
            await asyncio.sleep(1.0)
            for symbol in list(self.price_cache.keys()):
                old_price = self.price_cache[symbol]
                # Small percentage change: -0.08% to +0.08% per second
                pct_change = random.gauss(0.0, 0.0004)
                new_price = round(old_price * (1 + pct_change), 2)
                
                await self._process_tick(symbol, new_price)

    # ─────────────────────────────────────────────────────────────────────────
    #  LIVE MODE — Upstox V3 WebSocket + Protobuf
    # ─────────────────────────────────────────────────────────────────────────
    async def _get_authorized_uri(self) -> str | None:
        """
        Calls Upstox REST API to get the authorized WebSocket redirect URI.
        Returns the 'authorizedRedirectUri' string on success, None on failure.
        """
        if not self._access_token:
            print("[FEED] ERROR: No access token available. Cannot authorize WebSocket.")
            return None

        url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                if response.status_code != 200:
                    print(f"[FEED] Authorization failed: HTTP {response.status_code} — {response.text}")
                    return None
                data = response.json()
                uri = data.get("data", {}).get("authorizedRedirectUri")
                if not uri:
                    print(f"[FEED] Authorization response missing URI: {data}")
                    return None
                print(f"[FEED] WebSocket authorized. URI: {uri[:60]}...")
                return uri
        except Exception as e:
            print(f"[FEED] Exception during WebSocket authorization: {e}")
            return None

    def _build_subscription_message(self) -> str:
        """Builds the JSON subscription request payload per Upstox V3 spec."""
        return json.dumps({
            "guid": "stock-tool-feed-v1",
            "method": "sub",
            "data": {
                "instrumentKeys": INSTRUMENT_KEYS,
                "mode": "full",   # full = OHLCV + depth; ltpc = LTP only (lower bandwidth)
            }
        })

    def _decode_protobuf_tick(self, raw_bytes: bytes, pb2_module) -> list[dict]:
        """
        Decodes a binary Protobuf V3 frame into a list of price ticks.
        Returns list of {symbol, price} dicts.

        Proto structure (verified against upstox_client.feeder.proto.MarketDataFeedV3_pb2):
          FeedResponse.feeds  -> map<string, Feed>
          Feed.fullFeed       -> FullFeed  (for 'full' mode subscriptions)
          FullFeed.marketFF   -> MarketFullFeed
          MarketFullFeed.ltpc -> LTPC
          LTPC.ltp            -> float (Last Traded Price)
          Feed.ltpc           -> LTPC  (direct, for 'ltpc' mode subscriptions)
        """
        ticks = []
        try:
            feed_response = pb2_module.FeedResponse()
            feed_response.ParseFromString(raw_bytes)

            for key, feed in feed_response.feeds.items():
                ltp = 0.0

                # 'full' subscription mode — equity instruments
                if feed.HasField("fullFeed"):
                    ff = feed.fullFeed
                    if ff.HasField("marketFF") and ff.marketFF.HasField("ltpc"):
                        ltp = ff.marketFF.ltpc.ltp
                    elif ff.HasField("indexFF") and ff.indexFF.HasField("ltpc"):
                        ltp = ff.indexFF.ltpc.ltp

                # 'ltpc' subscription mode — lightweight LTP-only
                elif feed.HasField("ltpc"):
                    ltp = feed.ltpc.ltp

                if ltp and ltp > 0:
                    ticks.append({"symbol": key, "price": float(ltp)})

        except Exception as e:
            print(f"[FEED] Protobuf decode error: {e}")
        return ticks

    async def _run_rest_poll_feed(self):
        """
        Polls Upstox LTP REST API every 1s.
        Used as primary live feed for non-Plus accounts where WebSocket streaming is unavailable.
        """
        keys_encoded = "%2C".join(k.replace("|", "%7C") for k in INSTRUMENT_KEYS)
        url = f"https://api.upstox.com/v2/market-quote/ltp?instrument_key={keys_encoded}"
        print("[FEED] REST poll feed active (polling LTP every 1s).")
        while self.is_running:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(url, headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/json"
                    })
                    if r.status_code == 200:
                        data = r.json().get("data", {})
                        for quote in data.values():
                            ltp = quote.get("last_price", 0.0)
                            token = quote.get("instrument_token", "")
                            if ltp > 0 and token:
                                await self._process_tick(token, ltp)
                    else:
                        print(f"[FEED] REST poll HTTP {r.status_code}: {r.text[:120]}")
            except Exception as e:
                print(f"[FEED] REST poll error: {e}")
            await asyncio.sleep(1.0)

    async def _run_live_feed(self):
        """
        Connects to the real Upstox V3 WebSocket market data feed.
        Falls back to REST polling if no real ticks arrive within 30s (free-plan accounts
        get a subscription ack but no streaming data — REST polling works on all plans).
        """
        pb2 = _load_proto_module()
        if pb2 is None:
            print("[FEED] WARNING: upstox-python-sdk Protobuf module not found.")
            print("[FEED] Install it: pip install upstox-python-sdk")
            print("[FEED] Falling back to VIRTUAL simulator for safety.")
            await self._run_virtual_simulator()
            return

        print("[FEED] Protobuf module loaded. Connecting to Upstox live feed...")
        retry_delay = 5

        # Try WebSocket first; fall back to REST poll if no real ticks within 30s.
        real_tick_event = asyncio.Event()

        async def _ws_attempt():
            nonlocal retry_delay
            while self.is_running:
                try:
                    authorized_uri = await self._get_authorized_uri()
                    if not authorized_uri:
                        print(f"[FEED] Will retry authorization in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 60)
                        continue

                    ssl_context = ssl.create_default_context()
                    async with websockets.connect(
                        authorized_uri,
                        ssl=ssl_context,
                        ping_interval=20,
                        ping_timeout=10,
                    ) as ws:
                        print("[FEED] WebSocket connected. Sending subscription request...")
                        await ws.send(self._build_subscription_message())
                        print(f"[FEED] Subscribed to {len(INSTRUMENT_KEYS)} instruments.")
                        retry_delay = 5

                        async for message in ws:
                            if not self.is_running:
                                return
                            if isinstance(message, bytes):
                                ticks = self._decode_protobuf_tick(message, pb2)
                                for tick in ticks:
                                    real_tick_event.set()
                                    await self._process_tick(tick["symbol"], tick["price"])
                            elif isinstance(message, str):
                                try:
                                    payload = json.loads(message)
                                    if payload.get("type") == "error":
                                        print(f"[FEED] Server error: {payload}")
                                except json.JSONDecodeError:
                                    pass

                except websockets.exceptions.ConnectionClosedOK:
                    print("[FEED] WebSocket closed normally. Reconnecting...")
                except websockets.exceptions.ConnectionClosedError as e:
                    print(f"[FEED] WebSocket connection dropped: {e}. Reconnecting in {retry_delay}s...")
                except Exception as e:
                    print(f"[FEED] Unexpected error in live feed: {e}. Retrying in {retry_delay}s...")

                if self.is_running:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)

        ws_task = asyncio.create_task(_ws_attempt())

        # Give WebSocket 30s to deliver real ticks. If none, fall back to REST polling.
        try:
            await asyncio.wait_for(asyncio.shield(real_tick_event.wait()), timeout=30.0)
            print("[FEED] WebSocket streaming confirmed. Running in WS mode.")
            await ws_task  # keep WS running for the rest of the session
        except asyncio.TimeoutError:
            print("[FEED] No WS ticks in 30s (free plan). Cancelling WS, switching to REST polling.")
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            await self._run_rest_poll_feed()

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────
    def get_price(self, symbol: str) -> float:
        """Returns the current Last Traded Price (LTP) from the in-memory cache."""
        return self.price_cache.get(symbol, 0.0)

    def stop(self):
        """Gracefully stops the feed background worker."""
        self.is_running = False
        print("[FEED] Market feed stopped.")

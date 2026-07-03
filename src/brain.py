import asyncio
import concurrent.futures
import os
import random
import yfinance as yf
from google import genai
import aiohttp
import feedparser
import json
from src.config import Config

# Dedicated pool for Gemini calls. A plain asyncio.wait_for around asyncio.to_thread cannot
# actually interrupt a blocking call already running in a worker thread (asyncio cancellation
# is cooperative and synchronous code has no cancellation points) — the installed google-genai
# SDK also has no working HTTP-level timeout (see its _api_client.py: `timeout=None` is
# hardcoded pending a TODO). concurrent.futures.Future.result(timeout=...) is used instead,
# since it genuinely stops waiting after the deadline regardless of whether the submitted work
# has finished; the orphaned call keeps running in the pool but its result is simply discarded.
_gemini_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini")

def _call_with_hard_timeout(func, timeout, *args, **kwargs):
    future = _gemini_executor.submit(func, *args, **kwargs)
    return future.result(timeout=timeout)

class GeminiRateLimiter:
    """
    Token bucket rate limiter to ensure max 12 calls per 60 seconds.
    """
    def __init__(self, max_calls: int = 12, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.call_timestamps = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_running_loop().time()
            self.call_timestamps = [ts for ts in self.call_timestamps if now - ts < self.window_seconds]
            
            if len(self.call_timestamps) >= self.max_calls:
                oldest = self.call_timestamps[0]
                wait_time = self.window_seconds - (now - oldest)
                if wait_time > 0:
                    print(f"[RATE LIMITER] Limit reached. Sleeping for {wait_time:.2f} seconds...")
                    await asyncio.sleep(wait_time)
                
                now = asyncio.get_running_loop().time()
                self.call_timestamps = [ts for ts in self.call_timestamps if now - ts < self.window_seconds]

            self.call_timestamps.append(now)

async def call_gemini_with_retry(client, rate_limiter, prompt, schema, max_retries=3, call_timeout=180.0, max_quota_waits=3):
    backoff_base = 2
    attempt = 0
    quota_waits = 0
    while attempt < max_retries:
        try:
            await rate_limiter.acquire()
            response = await asyncio.to_thread(
                _call_with_hard_timeout,
                client.models.generate_content,
                call_timeout,
                model='gemini-3.1-flash-lite',
                contents=prompt,
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': schema,
                }
            )
            return json.loads(response.text)
        except concurrent.futures.TimeoutError:
            print(f"[GEMINI ERROR] Attempt {attempt+1} timed out after {call_timeout}s (orphaned call keeps running in background, result discarded).")
            if attempt < max_retries - 1:
                sleep_time = (backoff_base ** attempt) + random.uniform(0, 1)
                print(f"[GEMINI] Retrying in {sleep_time:.2f} seconds...")
                await asyncio.sleep(sleep_time)
            attempt += 1
        except Exception as e:
            error_msg = str(e).lower()
            print(f"[GEMINI ERROR] Attempt {attempt+1} failed: {e}")
            if "429" in error_msg or "quota" in error_msg:
                quota_waits += 1
                # This branch previously `continue`d without ever incrementing `attempt`,
                # so a persistently exhausted quota pinned the Cold Path in this 65s sleep
                # forever — max_retries never bounded it. max_quota_waits caps it separately.
                if quota_waits >= max_quota_waits:
                    print(f"[GEMINI RATE LIMIT] Quota still exhausted after {quota_waits} waits — giving up this cycle.")
                    return None
                print(f"[GEMINI RATE LIMIT] Quota hit ({quota_waits}/{max_quota_waits}). Sleeping for 65 seconds...")
                await asyncio.sleep(65.0)
                continue

            if attempt < max_retries - 1:
                sleep_time = (backoff_base ** attempt) + random.uniform(0, 1)
                print(f"[GEMINI] Retrying in {sleep_time:.2f} seconds...")
                await asyncio.sleep(sleep_time)
            attempt += 1
    return None

class NewsAgent:
    """
    Fetches raw news headlines from Yahoo Finance without calling LLM.
    """
    _schema_warning_shown = False

    def __init__(self):
        self.last_news_hash = ""

    def fetch_yf_news(self, ticker: str):
        try:
            stock = yf.Ticker(ticker)
            news_items = stock.news
            if not news_items:
                return []
            # yfinance 1.4.x nests the headline under item['content']['title'] — the older
            # flat item['title'] shape returns None for every article on this version and
            # silently starves FraudAgent (and the FLATTEN-on-fraud safety switch) of any
            # text to scan. Try the current shape first, fall back to the old flat shape in
            # case a future yfinance version reverts, and log loudly if both come up empty
            # so a schema drift like this is never silent again.
            titles = []
            for item in news_items[:5]:
                title = item.get('content', {}).get('title') if isinstance(item.get('content'), dict) else None
                if not title:
                    title = item.get('title')
                if title:
                    titles.append(title)

            if news_items and not titles and not NewsAgent._schema_warning_shown:
                NewsAgent._schema_warning_shown = True
                print(f"[NewsAgent WARNING] yfinance returned {len(news_items)} articles for {ticker} "
                      f"but no title could be extracted from either the content.title or title shape. "
                      f"yfinance's news schema may have changed again — sample item keys: {list(news_items[0].keys())}")

            return titles
        except Exception as e:
            print(f"[NewsAgent ERROR] Failed fetching news for {ticker}: {e}")
            return []

class FraudAgent:
    """
    Deterministic string matching for governance / fraud red flags in news.
    Tuned for Indian markets (SEBI/RBI/ED/NCLT specific signals).
    """
    def __init__(self):
        self.red_flags = [
            # Generic governance
            "fraud", "scam", "embezzle", "siphon", "shell company", "related party",
            # Auditor / accounting
            "auditor resign", "forensic audit", "qualified opinion", "restate accounts",
            # Regulator action (India-specific)
            "sebi ban", "sebi penalty", "sebi order", "sebi probe", "sebi bars",
            "rbi penalty", "rbi action", "rbi restriction",
            "ed raid", "enforcement directorate", "income tax raid", "cbi", "sfio",
            # Promoter / debt distress
            "promoter pledge", "pledged shares", "promoter selling", "stake sale by promoter",
            "wilful defaulter", "loan default", "debt default", "npa", "bad loans",
            "nclt", "insolvency", "ibc", "bankruptcy",
            # Misc
            "investigation", "money laundering", "accounting irregular",
        ]

    def run(self, news_data):
        print("[FraudAgent] Scanning news for governance red flags...")
        fraud_flags = {}
        for sym, headlines in news_data.items():
            fraud_detected = False
            for hl in headlines:
                text = hl.lower()
                if any(flag in text for flag in self.red_flags):
                    print(f"    [!] FRAUD FLAG DETECTED for {sym}: '{hl}'")
                    fraud_detected = True
                    break
            fraud_flags[sym] = fraud_detected
        return fraud_flags

class CommitteeAgent:
    """
    Single-shot Gemini call that replaces SentimentAnalyst, MacroAnalyst, and RiskGatekeeper.
    Evaluates everything in one prompt to save API quota and latency.
    """
    def __init__(self, client, rate_limiter):
        self.client = client
        self.rate_limiter = rate_limiter

    async def run(self, global_headlines, news_data, fraud_data, technical_data, financial_data):
        print("[CommitteeAgent] Evaluating Sentiment, Macro, and Gates via single AI call...")
        schema = {
            'type': 'OBJECT',
            'properties': {
                'macro_mood': {'type': 'STRING', 'description': 'BULLISH, NEUTRAL, or BEARISH'},
                'macro_reasoning': {'type': 'STRING'},
                'gates': {
                    'type': 'ARRAY',
                    'items': {
                        'type': 'OBJECT',
                        'properties': {
                            'ticker': {'type': 'STRING'},
                            'gate': {'type': 'STRING', 'description': 'ALLOW_BUY, ALLOW_SELL, ALLOW_ALL, VETO, or FLATTEN'},
                            'conviction_score': {'type': 'NUMBER', 'description': '0.0 to 1.0 representing confidence'},
                            'sentiment': {'type': 'NUMBER', 'description': '-1.0 to 1.0 based on news'},
                            'reasoning': {'type': 'STRING', 'description': 'Why this gate was chosen based on tech, news, and fraud signals.'}
                        },
                        'required': ['ticker', 'gate', 'conviction_score', 'sentiment', 'reasoning']
                    }
                }
            },
            'required': ['macro_mood', 'macro_reasoning', 'gates']
        }
        
        prompt = "You are an elite, multi-agent AI trading committee (Sentiment Analyst, Macro Economist, Risk Manager) "
        prompt += "for a virtual paper-trading account with a large capital base. Your job is to evaluate the provided "
        prompt += "inputs and decide the final action gate for each symbol. Output JSON as requested.\n\n"
        prompt += f"AVAILABLE CASH FOR NEW TRADES: Rs. {financial_data.get('available_balance', 0):.2f}\n"
        prompt += f"CURRENT OPEN POSITIONS: {json.dumps(financial_data.get('open_positions', []))}\n"
        prompt += f"GLOBAL MACRO HEADLINES:\n{chr(10).join(global_headlines) if global_headlines else 'None'}\n\n"

        prompt += "Guidelines for Action Gate:\n"
        prompt += "- This is virtual money used to test a real trading strategy. Default to ALLOW_ALL unless you see a\n"
        prompt += "  concrete reason not to trade this symbol — do not withhold participation just because sentiment\n"
        prompt += "  is neutral or unremarkable. A quiet news day is not itself a reason to veto.\n"
        prompt += "- Reserve VETO for a real red flag on this specific symbol: fraud detected, clearly bearish news,\n"
        prompt += "  or an extreme/contradictory technical reading. Do not VETO purely for low available cash unless\n"
        prompt += "  it is genuinely near zero.\n"
        prompt += "- If sentiment is extremely negative, a crash is imminent, or FRAUD DETECTED is true, output FLATTEN.\n"
        prompt += "- Consider the last 10 RSI and MACD values for trend direction. If RSI is > 70 (overbought) -> favor ALLOW_SELL. If RSI < 30 (oversold) -> favor ALLOW_BUY.\n\n"

        prompt += "PER-SYMBOL DATA:\n"
        for sym, headlines in news_data.items():
            base_symbol = sym.replace(".NS", "")
            fraud_detected = fraud_data.get(sym, False)
            tech = technical_data.get(sym, {})

            prompt += f"TICKER: {base_symbol}\n"
            prompt += f"FRAUD DETECTED: {fraud_detected}\n"
            prompt += f"NEWS HEADLINES:\n{chr(10).join(headlines) if headlines else 'No news'}\n"
            prompt += f"RSI (Last 10 5-min candles): {tech.get('RSI', [])}\n"
            prompt += f"MACD (Last 10 5-min candles): {tech.get('MACD', [])}\n\n"
            
        result = await call_gemini_with_retry(self.client, self.rate_limiter, prompt, schema)
        gates = {}
        reasoning = {}
        if result:
            print(f"[COMMITTEE] Macro Mood: {result.get('macro_mood')} | Reasoning: {result.get('macro_reasoning')}")
            if 'gates' in result:
                for item in result['gates']:
                    ticker = item['ticker']
                    gate = item.get('gate', 'VETO').upper()
                    gates[ticker] = gate
                    reasoning[ticker] = item.get('reasoning', '')
                    print(f"[COMMITTEE] {ticker} -> Gate: {gate} | Conviction: {item.get('conviction_score')} | Sentiment: {item.get('sentiment')} | Reason: {item.get('reasoning')}")
        else:
            print("[COMMITTEE] LLM call failed, falling back to VETO.")
            for sym in news_data.keys():
                gates[sym.replace(".NS", "")] = "VETO"

        return gates, reasoning

class BrainEngine:
    def __init__(self):
        self.watch_symbols = list(Config.SYMBOL_MAP.keys())
        self.gates = {s.replace(".NS", ""): "VETO" for s in self.watch_symbols}
        self.reasoning = {s.replace(".NS", ""): "" for s in self.watch_symbols}
        self.global_headlines = []
        
        # Initialize Gemini Client and pipeline
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("WARNING: GEMINI_API_KEY not found in env. Cold Path AI analysis is disabled.")
            self.client = None
            self.rate_limiter = None
            self.news_agent = None
            self.fraud_agent = None
            self.committee_agent = None
        else:
            self.client = genai.Client(api_key=api_key)
            self.rate_limiter = GeminiRateLimiter(max_calls=12, window_seconds=60.0)
            self.news_agent = NewsAgent()
            self.fraud_agent = FraudAgent()
            self.committee_agent = CommitteeAgent(self.client, self.rate_limiter)

    async def run(self, technicals):
        """
        The COLD PATH (1-15s)
        Runs asynchronously every 300 seconds (5 minutes) to fetch news and query Gemini.
        Uses a Single-Shot Committee Agent for analysis.
        """
        print("Starting Single-Shot Committee Cold Path AI Engine...")
        if not self.client:
            self.gates = {s.replace(".NS", ""): "ALLOW_ALL" for s in self.watch_symbols}
            return
            
        from src.orders import get_available_balance, _open_positions

        while True:
            # Skip AI evaluation entirely outside NSE trading hours — no point burning
            # Gemini quota overnight/weekends when no trades can execute anyway.
            if not Config.is_market_hours():
                self.gates = {s.replace(".NS", ""): "VETO" for s in self.watch_symbols}
                await asyncio.sleep(60.0)
                continue

            # 1. Fetch Raw News (in parallel — sequential fetch across 40 symbols was pushing
            # a single Cold Path cycle past a minute before Gemini was even called)
            news_results = await asyncio.gather(
                *(asyncio.to_thread(self.news_agent.fetch_yf_news, sym) for sym in self.watch_symbols)
            )
            news_data = dict(zip(self.watch_symbols, news_results))

            # 2. Detect Fraud (Deterministic)
            fraud_data = self.fraud_agent.run(news_data)

            # 3. Extract Technical Data
            tech_data = {}
            for sym in self.watch_symbols:
                upstox_sym = Config.SYMBOL_MAP.get(sym)
                if upstox_sym and upstox_sym in technicals.indicators:
                    inds = technicals.indicators[upstox_sym]
                    # talipp pads its output lists with None during the indicator warmup period,
                    # so drop None entries before reading values / attributes.
                    rsi_history = [round(r, 2) for r in inds['rsi'][-10:] if r is not None]
                    macd_history = [round(m.macd, 4) for m in inds['macd'][-10:] if m is not None and m.macd is not None]
                    tech_data[sym] = {"RSI": rsi_history, "MACD": macd_history}
            
            # 4. Extract Financial Data — get_available_balance() does synchronous SQLite
            # reads and must not run directly on the event loop.
            fin_data = {
                "available_balance": await asyncio.to_thread(get_available_balance),
                "open_positions": list(_open_positions.values())
            }

            # 5. Single-Shot AI Evaluation
            new_gates, new_reasoning = await self.committee_agent.run(
                self.global_headlines,
                news_data,
                fraud_data,
                tech_data,
                fin_data
            )

            # Update instance state safely
            for base_sym, gate in new_gates.items():
                if base_sym in self.gates:
                    self.gates[base_sym] = gate
            for base_sym, reason in new_reasoning.items():
                if base_sym in self.gates:
                    self.reasoning[base_sym] = reason

            await asyncio.sleep(300.0) # Refresh every 5 minutes

    async def poll_rss_feeds(self):
        rss_url = "https://www.livemint.com/rss/markets"
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(rss_url) as resp:
                        content = await resp.text()
                
                d = await asyncio.to_thread(feedparser.parse, content)
                self.global_headlines = [entry.title for entry in d.entries[:10]]
                print(f"[BRAIN] Fetched {len(self.global_headlines)} global headlines from Livemint")
            except Exception as e:
                print(f"[BRAIN] Error polling RSS feeds: {e}")
            
            await asyncio.sleep(300) # Poll every 5 minutes

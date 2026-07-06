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

# Watchlist symbols per Gemini committee call. Concurrent batches of this size keep per-call
# latency close to what's empirically proven to work (~40 symbols took 2-3 minutes) instead of
# one call covering the whole (now 200-symbol) watchlist taking many minutes and risking the
# hard-timeout below. See BrainEngine.run() for how batches are gathered concurrently.
COMMITTEE_BATCH_SIZE = 40

# Dedicated pool for Gemini calls. A plain asyncio.wait_for around asyncio.to_thread cannot
# actually interrupt a blocking call already running in a worker thread (asyncio cancellation
# is cooperative and synchronous code has no cancellation points) — the installed google-genai
# SDK also has no working HTTP-level timeout (see its _api_client.py: `timeout=None` is
# hardcoded pending a TODO). concurrent.futures.Future.result(timeout=...) is used instead,
# since it genuinely stops waiting after the deadline regardless of whether the submitted work
# has finished; the orphaned call keeps running in the pool but its result is simply discarded.
# max_workers sized for a full watchlist's worth of concurrent committee batches (200 symbols /
# COMMITTEE_BATCH_SIZE = 5 batches) without any of them queueing behind another.
_gemini_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="gemini")

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

        # Group currently open positions by base ticker so each symbol's own PER-SYMBOL DATA
        # block below can show its position inline — the model reasons much better about
        # "should I tighten THIS position" when it's right next to that symbol's own
        # technicals, rather than in one combined JSON blob at the top of the prompt.
        upstox_to_base = {v: k.replace(".NS", "") for k, v in Config.SYMBOL_MAP.items()}
        positions_by_base = {}
        for pos in financial_data.get('open_positions', []):
            base = upstox_to_base.get(pos.get('symbol'))
            if base:
                positions_by_base.setdefault(base, []).append(pos)

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
                            'reasoning': {'type': 'STRING', 'description': 'Why this gate was chosen based on tech, news, and fraud signals.'},
                            'suggested_target': {'type': 'NUMBER', 'description': 'Only if this symbol has an open position and you want to tighten its target closer to lock in a smaller win sooner. Omit/null otherwise.'},
                            'suggested_stop_loss': {'type': 'NUMBER', 'description': 'Only if this symbol has an open position and you want to tighten its stop closer to protect more of the current gain. Omit/null otherwise.'}
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
        prompt += f"GLOBAL MACRO HEADLINES:\n{chr(10).join(global_headlines) if global_headlines else 'None'}\n\n"

        prompt += "Guidelines for Action Gate:\n"
        prompt += "- This is virtual money used to test a real trading strategy. Default to ALLOW_ALL unless you see a\n"
        prompt += "  concrete reason not to trade this symbol — do not withhold participation just because sentiment\n"
        prompt += "  is neutral or unremarkable. A quiet news day is not itself a reason to veto.\n"
        prompt += "- Reserve VETO for a real red flag on this specific symbol: fraud detected, clearly bearish news,\n"
        prompt += "  or an extreme/contradictory technical reading. Do not VETO purely for low available cash unless\n"
        prompt += "  it is genuinely near zero.\n"
        prompt += "- If sentiment is extremely negative, a crash is imminent, or FRAUD DETECTED is true, output FLATTEN.\n"
        prompt += "- Consider the last 10 RSI and MACD values for trend direction. If RSI is > 70 (overbought) -> favor ALLOW_SELL. If RSI < 30 (oversold) -> favor ALLOW_BUY.\n"
        prompt += "- The strategy here is fast, frequent, small-profit trades — capital tied up for hours in one slow\n"
        prompt += "  trade is capital that can't be used on a better signal elsewhere. If a symbol below shows an OPEN\n"
        prompt += "  POSITION and momentum looks like it's fading before reaching target, suggest tightening its target\n"
        prompt += "  (suggested_target) closer to the current price to lock in a smaller win sooner, and/or tightening\n"
        prompt += "  its stop (suggested_stop_loss) to protect more of any current gain. Only ever suggest moving these\n"
        prompt += "  CLOSER to the current price, in the direction that reduces risk or time-to-exit — never suggest\n"
        prompt += "  loosening either one further away; the system will reject any suggestion that tries to.\n\n"

        prompt += "PER-SYMBOL DATA:\n"
        for sym, headlines in news_data.items():
            base_symbol = sym.replace(".NS", "")
            fraud_detected = fraud_data.get(sym, False)
            tech = technical_data.get(sym, {})

            prompt += f"TICKER: {base_symbol}\n"
            prompt += f"FRAUD DETECTED: {fraud_detected}\n"
            prompt += f"NEWS HEADLINES:\n{chr(10).join(headlines) if headlines else 'No news'}\n"
            prompt += f"RSI (Last 10 5-min candles): {tech.get('RSI', [])}\n"
            prompt += f"MACD (Last 10 5-min candles): {tech.get('MACD', [])}\n"
            if base_symbol in positions_by_base:
                prompt += f"OPEN POSITION(S): {json.dumps(positions_by_base[base_symbol])}\n"
            prompt += "\n"

        result = await call_gemini_with_retry(self.client, self.rate_limiter, prompt, schema)
        gates = {}
        reasoning = {}
        adjustments = {}
        if result:
            print(f"[COMMITTEE] Macro Mood: {result.get('macro_mood')} | Reasoning: {result.get('macro_reasoning')}")
            if 'gates' in result:
                for item in result['gates']:
                    ticker = item['ticker']
                    gate = item.get('gate', 'VETO').upper()
                    gates[ticker] = gate
                    reasoning[ticker] = item.get('reasoning', '')
                    sug_target = item.get('suggested_target')
                    sug_stop = item.get('suggested_stop_loss')
                    if sug_target is not None or sug_stop is not None:
                        adjustments[ticker] = {"target": sug_target, "stop_loss": sug_stop}
                    print(f"[COMMITTEE] {ticker} -> Gate: {gate} | Conviction: {item.get('conviction_score')} | Sentiment: {item.get('sentiment')} | Reason: {item.get('reasoning')}")
        else:
            print("[COMMITTEE] LLM call failed, falling back to VETO.")
            for sym in news_data.keys():
                gates[sym.replace(".NS", "")] = "VETO"

        return gates, reasoning, adjustments

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
            
        from src.orders import get_available_balance, _open_positions, adjust_open_positions_for_symbol

        while True:
            # Skip AI evaluation entirely outside NSE trading hours — no point burning
            # Gemini quota overnight/weekends when no trades can execute anyway.
            if not Config.is_market_hours():
                self.gates = {s.replace(".NS", ""): "VETO" for s in self.watch_symbols}
                await asyncio.sleep(60.0)
                continue

            # 1. Fetch Raw News (in parallel — sequential fetch was pushing a single Cold Path
            # cycle past a minute before Gemini was even called at 40 symbols; capped at 20
            # concurrent requests now that the watchlist is 200 symbols, to avoid hammering
            # Yahoo Finance with 200 simultaneous requests from one process).
            news_semaphore = asyncio.Semaphore(20)

            async def _fetch_news(sym):
                async with news_semaphore:
                    return await asyncio.to_thread(self.news_agent.fetch_yf_news, sym)

            news_results = await asyncio.gather(*(_fetch_news(sym) for sym in self.watch_symbols))
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

            # 5. AI Evaluation — batched into concurrent calls rather than one shot.
            # With 200 watchlist symbols, a single Gemini call covering all of them would take
            # many minutes (a 40-symbol call alone took 2-3 minutes empirically) and risk
            # exceeding even the hard-timeout in call_gemini_with_retry. Splitting into
            # COMMITTEE_BATCH_SIZE-symbol chunks run CONCURRENTLY keeps per-call latency close
            # to what's already proven to work, while covering the full watchlist in roughly
            # the same wall-clock time as a single batch would take alone.
            batches = [self.watch_symbols[i:i + COMMITTEE_BATCH_SIZE] for i in range(0, len(self.watch_symbols), COMMITTEE_BATCH_SIZE)]

            async def _run_batch(batch_symbols):
                batch_news = {s: news_data[s] for s in batch_symbols if s in news_data}
                batch_fraud = {s: fraud_data[s] for s in batch_symbols if s in fraud_data}
                batch_tech = {s: tech_data[s] for s in batch_symbols if s in tech_data}
                return await self.committee_agent.run(self.global_headlines, batch_news, batch_fraud, batch_tech, fin_data)

            batch_results = await asyncio.gather(*(_run_batch(b) for b in batches))

            new_gates, new_reasoning, new_adjustments = {}, {}, {}
            for gates_part, reasoning_part, adjustments_part in batch_results:
                new_gates.update(gates_part)
                new_reasoning.update(reasoning_part)
                new_adjustments.update(adjustments_part)

            # Update instance state safely
            for base_sym, gate in new_gates.items():
                if base_sym in self.gates:
                    self.gates[base_sym] = gate
            for base_sym, reason in new_reasoning.items():
                if base_sym in self.gates:
                    self.reasoning[base_sym] = reason

            # 6. Apply any tightening suggestions to positions already open on these symbols.
            # adjust_open_positions_for_symbol() re-validates every suggestion itself (can
            # only tighten, never loosen) — this call is just wiring the AI's output through,
            # not a trust boundary.
            for base_sym, adj in new_adjustments.items():
                yf_sym = f"{base_sym}.NS"
                upstox_sym = Config.SYMBOL_MAP.get(yf_sym)
                if upstox_sym:
                    await adjust_open_positions_for_symbol(upstox_sym, adj.get("target"), adj.get("stop_loss"))

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

import asyncio
import os
import random
import yfinance as yf
from google import genai
import aiohttp
import feedparser
import json
import pandas as pd
from datetime import datetime, timezone
import hashlib

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

async def call_gemini_with_retry(client, rate_limiter, prompt, schema, max_retries=3):
    backoff_base = 2
    attempt = 0
    while attempt < max_retries:
        try:
            await rate_limiter.acquire()
            response = await asyncio.to_thread(
                client.models.generate_content,
                model='gemini-3.1-flash-lite',
                contents=prompt,
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': schema,
                }
            )
            return json.loads(response.text)
        except Exception as e:
            error_msg = str(e).lower()
            print(f"[GEMINI ERROR] Attempt {attempt+1} failed: {e}")
            if "429" in error_msg or "quota" in error_msg:
                print("[GEMINI RATE LIMIT] Quota hit. Sleeping for 65 seconds...")
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
    def __init__(self):
        self.last_news_hash = ""

    def fetch_yf_news(self, ticker: str):
        try:
            stock = yf.Ticker(ticker)
            news_items = stock.news
            if not news_items:
                return []
            return [item.get('title', '') for item in news_items[:5]]
        except Exception as e:
            print(f"[NewsAgent ERROR] Failed fetching news for {ticker}: {e}")
            return []

class EarningsAgent:
    """
    Checks YFinance for PEAD (Post-Earnings Announcement Drift) and Earnings Risk.
    Deterministic, no LLM calls.
    """
    def __init__(self):
        pass
        
    def run(self, watch_symbols):
        print("[EarningsAgent] Scanning for PEAD signals and earnings risk...")
        earnings_data = {}
        now = datetime.now(timezone.utc)
        
        for sym in watch_symbols:
            try:
                t = yf.Ticker(sym)
                df = t.earnings_dates
                pead_status = "NEUTRAL"
                
                if df is not None and not df.empty:
                    for dt, row in df.iterrows():
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                            
                        delta_days = (dt - now).total_seconds() / 86400.0
                        
                        if 0 <= delta_days <= 3:
                            pead_status = "EARNINGS_RISK"
                            break
                        elif -7 <= delta_days < 0:
                            surprise = row.get("Surprise(%)", 0)
                            if pd.notna(surprise):
                                if surprise > 0:
                                    pead_status = "PEAD_BULLISH"
                                elif surprise < 0:
                                    pead_status = "PEAD_BEARISH"
                            break
                            
                earnings_data[sym] = pead_status
            except Exception as e:
                print(f"    [!] Error fetching earnings for {sym}: {e}")
                earnings_data[sym] = "NEUTRAL"
                
        return earnings_data

class FraudAgent:
    """
    Deterministic string matching for SEBI/Fraud red flags in news.
    """
    def __init__(self):
        self.red_flags = ["fraud", "sebi", "scam", "investigation", "auditor resign", "default", "penalty"]

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

    async def run(self, global_headlines, news_data, pead_data, fraud_data, technical_data, financial_data):
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
                            'reasoning': {'type': 'STRING', 'description': 'Why this gate was chosen based on tech, news, fraud, PEAD.'}
                        },
                        'required': ['ticker', 'gate', 'conviction_score', 'sentiment', 'reasoning']
                    }
                }
            },
            'required': ['macro_mood', 'macro_reasoning', 'gates']
        }
        
        prompt = "You are an elite, multi-agent AI trading committee (Sentiment Analyst, Macro Economist, Risk Manager). "
        prompt += "Your job is to evaluate the provided inputs and decide the final action gate for each symbol. Output JSON as requested.\n\n"
        prompt += f"AVAILABLE CASH FOR NEW TRADES: Rs. {financial_data.get('available_balance', 0):.2f}\n"
        prompt += f"CURRENT OPEN POSITIONS: {json.dumps(financial_data.get('open_positions', []))}\n"
        prompt += f"GLOBAL MACRO HEADLINES:\n{chr(10).join(global_headlines) if global_headlines else 'None'}\n\n"
        
        prompt += "Guidelines for Action Gate:\n"
        prompt += "- If sentiment is extremely negative, a crash is imminent, or FRAUD DETECTED is true, output FLATTEN.\n"
        prompt += "- If EARNINGS_RISK is present (upcoming earnings), output VETO to avoid volatility.\n"
        prompt += "- If PEAD_BULLISH is present (recent earnings beat) and technicals are good, assign a higher conviction to ALLOW_BUY.\n"
        prompt += "- If available cash is very low and sentiment is weak, output VETO to preserve capital.\n"
        prompt += "- Consider the last 10 RSI and MACD values for trend direction. If RSI is > 70 (overbought) -> favor ALLOW_SELL or VETO. If RSI < 30 (oversold) -> favor ALLOW_BUY.\n\n"
        
        prompt += "PER-SYMBOL DATA:\n"
        for sym, headlines in news_data.items():
            base_symbol = sym.replace(".NS", "")
            pead_status = pead_data.get(sym, "NEUTRAL")
            fraud_detected = fraud_data.get(sym, False)
            tech = technical_data.get(sym, {})
            
            prompt += f"TICKER: {base_symbol}\n"
            prompt += f"PEAD STATUS: {pead_status}\n"
            prompt += f"FRAUD DETECTED: {fraud_detected}\n"
            prompt += f"NEWS HEADLINES:\n{chr(10).join(headlines) if headlines else 'No news'}\n"
            prompt += f"RSI (Last 10 5-min candles): {tech.get('RSI', [])}\n"
            prompt += f"MACD (Last 10 5-min candles): {tech.get('MACD', [])}\n\n"
            
        result = await call_gemini_with_retry(self.client, self.rate_limiter, prompt, schema)
        gates = {}
        if result:
            print(f"[COMMITTEE] Macro Mood: {result.get('macro_mood')} | Reasoning: {result.get('macro_reasoning')}")
            if 'gates' in result:
                for item in result['gates']:
                    ticker = item['ticker']
                    gate = item.get('gate', 'VETO').upper()
                    gates[ticker] = gate
                    print(f"[COMMITTEE] {ticker} -> Gate: {gate} | Conviction: {item.get('conviction_score')} | Sentiment: {item.get('sentiment')} | Reason: {item.get('reasoning')}")
        else:
            print("[COMMITTEE] LLM call failed, falling back to VETO.")
            for sym in news_data.keys():
                gates[sym.replace(".NS", "")] = "VETO"
                
        return gates

class BrainEngine:
    def __init__(self):
        self.watch_symbols = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "SBIN.NS", "AXISBANK.NS"
        ]
        self.gates = {s.replace(".NS", ""): "VETO" for s in self.watch_symbols}
        self.global_headlines = []
        
        # Initialize Gemini Client and pipeline
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("WARNING: GEMINI_API_KEY not found in env. Cold Path AI analysis is disabled.")
            self.client = None
            self.rate_limiter = None
            self.news_agent = None
            self.earnings_agent = None
            self.fraud_agent = None
            self.committee_agent = None
        else:
            self.client = genai.Client(api_key=api_key)
            self.rate_limiter = GeminiRateLimiter(max_calls=12, window_seconds=60.0)
            self.news_agent = NewsAgent()
            self.earnings_agent = EarningsAgent()
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
        from src.config import Config
            
        while True:
            # 1. Fetch Raw News
            news_data = {}
            for sym in self.watch_symbols:
                news_data[sym] = await asyncio.to_thread(self.news_agent.fetch_yf_news, sym)
                
            # 2. Fetch Earnings PEAD (Deterministic)
            pead_data = await asyncio.to_thread(self.earnings_agent.run, self.watch_symbols)
            
            # 3. Detect Fraud (Deterministic)
            fraud_data = self.fraud_agent.run(news_data)
            
            # 4. Extract Technical Data
            tech_data = {}
            for sym in self.watch_symbols:
                upstox_sym = Config.SYMBOL_MAP.get(sym)
                if upstox_sym and upstox_sym in technicals.indicators:
                    inds = technicals.indicators[upstox_sym]
                    rsi_history = inds['rsi'][-10:] if len(inds['rsi']) >= 10 else inds['rsi'][:]
                    macd_history = [m.macd for m in inds['macd'][-10:]] if len(inds['macd']) >= 10 else [m.macd for m in inds['macd'][:]]
                    tech_data[sym] = {"RSI": rsi_history, "MACD": macd_history}
            
            # 5. Extract Financial Data
            fin_data = {
                "available_balance": get_available_balance(),
                "open_positions": list(_open_positions.values())
            }
            
            # 6. Single-Shot AI Evaluation
            new_gates = await self.committee_agent.run(
                self.global_headlines, 
                news_data, 
                pead_data, 
                fraud_data, 
                tech_data, 
                fin_data
            )
            
            # Update instance state safely
            for base_sym, gate in new_gates.items():
                if base_sym in self.gates:
                    self.gates[base_sym] = gate

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

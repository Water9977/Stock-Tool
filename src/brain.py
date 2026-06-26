import asyncio
import os
import random
import yfinance as yf
from google import genai
import aiohttp
import feedparser
import json

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
            # Remove timestamps older than window
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
    def __init__(self, client, rate_limiter):
        self.client = client
        self.rate_limiter = rate_limiter
        self.cached_combined_news = ""
        self.cached_output = {}

    def _fetch_yf_news(self, ticker: str):
        stock = yf.Ticker(ticker)
        news_items = stock.news
        if not news_items:
            return []
        return [item.get('title', '') for item in news_items[:5]]

    async def run(self, watch_symbols, global_headlines):
        symbol_news = {}
        for yf_symbol in watch_symbols:
            try:
                headlines = await asyncio.to_thread(self._fetch_yf_news, yf_symbol)
                symbol_news[yf_symbol] = headlines
            except Exception as e:
                print(f"[NewsAgent ERROR] Failed fetching news for {yf_symbol}: {e}")
                symbol_news[yf_symbol] = []
        
        combined_news_str = f"GLOBAL:{'|'.join(global_headlines)};"
        for sym, headlines in symbol_news.items():
            combined_news_str += f"{sym}:{'|'.join(headlines)};"

        if self.cached_combined_news == combined_news_str and self.cached_output:
            print("[NewsAgent] News unchanged. Using cached headlines.")
            return self.cached_output

        print("[NewsAgent] Extracting top headlines via LLM...")
        schema = {
            'type': 'OBJECT',
            'properties': {
                'symbols': {
                    'type': 'ARRAY',
                    'items': {
                        'type': 'OBJECT',
                        'properties': {
                            'ticker': {'type': 'STRING'},
                            'top_headlines': {
                                'type': 'ARRAY',
                                'items': {'type': 'STRING'}
                            }
                        },
                        'required': ['ticker', 'top_headlines']
                    }
                }
            },
            'required': ['symbols']
        }

        prompt = "Extract the top 3 most relevant headlines per symbol from the following raw data. Format as requested.\n"
        prompt += f"GLOBAL MARKET HEADLINES:\n{chr(10).join(global_headlines) if global_headlines else 'None'}\n\n"
        for sym, h in symbol_news.items():
            prompt += f"RAW HEADLINES FOR {sym}:\n{chr(10).join(h) if h else 'None'}\n\n"

        result = await call_gemini_with_retry(self.client, self.rate_limiter, prompt, schema)
        if result and 'symbols' in result:
            output = {item['ticker']: item['top_headlines'] for item in result['symbols']}
            # Ensure all requested symbols exist
            for sym in watch_symbols:
                if sym not in output:
                    output[sym] = []
            self.cached_combined_news = combined_news_str
            self.cached_output = output
            return output
        
        return {sym: [] for sym in watch_symbols}

class SentimentAnalyst:
    def __init__(self, client, rate_limiter):
        self.client = client
        self.rate_limiter = rate_limiter

    async def run(self, news_data):
        print("[SentimentAnalyst] Scoring sentiment...")
        schema = {
            'type': 'OBJECT',
            'properties': {
                'sentiments': {
                    'type': 'ARRAY',
                    'items': {
                        'type': 'OBJECT',
                        'properties': {
                            'ticker': {'type': 'STRING'},
                            'score': {'type': 'NUMBER', 'description': '-1.0 to 1.0'},
                            'reasoning': {'type': 'STRING'}
                        },
                        'required': ['ticker', 'score', 'reasoning']
                    }
                }
            },
            'required': ['sentiments']
        }
        
        prompt = "Analyze the sentiment for the following tickers based on their top headlines. Score from -1.0 (very negative) to 1.0 (very positive).\n\n"
        for sym, headlines in news_data.items():
            prompt += f"TICKER: {sym}\nHEADLINES:\n{chr(10).join(headlines) if headlines else 'No news'}\n\n"
            
        result = await call_gemini_with_retry(self.client, self.rate_limiter, prompt, schema)
        output = {}
        if result and 'sentiments' in result:
            for item in result['sentiments']:
                output[item['ticker']] = {
                    'score': float(item.get('score', 0.0)),
                    'reasoning': item.get('reasoning', '')
                }
        
        for sym in news_data.keys():
            if sym not in output:
                output[sym] = {'score': 0.0, 'reasoning': 'Failed to fetch sentiment'}
        return output

class MacroAnalyst:
    def __init__(self, client, rate_limiter):
        self.client = client
        self.rate_limiter = rate_limiter

    async def run(self, global_headlines):
        print("[MacroAnalyst] Assessing market mood...")
        schema = {
            'type': 'OBJECT',
            'properties': {
                'mood': {'type': 'STRING', 'description': 'BULLISH, NEUTRAL, or BEARISH'},
                'score': {'type': 'NUMBER', 'description': '-1.0 to 1.0'},
                'key_themes': {
                    'type': 'ARRAY',
                    'items': {'type': 'STRING'}
                }
            },
            'required': ['mood', 'score', 'key_themes']
        }
        
        prompt = "Assess the overall market mood based on these global headlines. Provide mood, score, and key themes.\n\n"
        prompt += f"HEADLINES:\n{chr(10).join(global_headlines) if global_headlines else 'None'}\n"
        
        result = await call_gemini_with_retry(self.client, self.rate_limiter, prompt, schema)
        if result:
            return {
                'mood': result.get('mood', 'NEUTRAL').upper(),
                'score': float(result.get('score', 0.0)),
                'key_themes': result.get('key_themes', [])
            }
            
        return {'mood': 'NEUTRAL', 'score': 0.0, 'key_themes': []}

class RiskGatekeeper:
    def __init__(self):
        pass

    def run(self, sentiment_data, macro_data):
        print("[RiskGatekeeper] Evaluating gates...")
        gates = {}
        macro_mood = macro_data.get('mood', 'NEUTRAL')
        
        for sym, data in sentiment_data.items():
            base_symbol = sym.replace(".NS", "")
            sentiment_score = data.get('score', 0.0)
            
            gate = "ALLOW_ALL"
            if macro_mood == "BEARISH" and sentiment_score < -0.7:
                gate = "FLATTEN"
            elif sentiment_score < -0.5 and macro_mood in ["NEUTRAL", "BEARISH"]:
                gate = "ALLOW_SELL"
            elif macro_mood == "BEARISH" and sentiment_score < -0.3:
                gate = "VETO"
            elif sentiment_score > 0.5 and macro_mood in ["NEUTRAL", "BULLISH"]:
                gate = "ALLOW_BUY"
                    
            gates[base_symbol] = gate
            print(f"[GATEKEEPER] {sym} -> Gate: {gate} | Sent: {sentiment_score:.2f} | Macro: {macro_mood}")
            
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
            self.sentiment_analyst = None
            self.macro_analyst = None
            self.risk_gatekeeper = None
        else:
            self.client = genai.Client(api_key=api_key)
            self.rate_limiter = GeminiRateLimiter(max_calls=12, window_seconds=60.0)
            self.news_agent = NewsAgent(self.client, self.rate_limiter)
            self.sentiment_analyst = SentimentAnalyst(self.client, self.rate_limiter)
            self.macro_analyst = MacroAnalyst(self.client, self.rate_limiter)
            self.risk_gatekeeper = RiskGatekeeper()

    async def run(self):
        """
        The COLD PATH (1-15s)
        Runs asynchronously every 300 seconds (5 minutes) to fetch news and query Gemini.
        Uses a 4-agent pipeline for analysis.
        """
        print("Starting Multi-Agent Cold Path AI Sentiment Engine...")
        if not self.client:
            self.gates = {s.replace(".NS", ""): "ALLOW_ALL" for s in self.watch_symbols}
            return
            
        while True:
            # Pipeline Execution
            news_data = await self.news_agent.run(self.watch_symbols, self.global_headlines)
            
            sentiment, macro = await asyncio.gather(
                self.sentiment_analyst.run(news_data),
                self.macro_analyst.run(self.global_headlines)
            )
            
            new_gates = self.risk_gatekeeper.run(sentiment, macro)
            
            # Update instance state safely
            for base_sym, gate in new_gates.items():
                if base_sym in self.gates:
                    self.gates[base_sym] = gate

            await asyncio.sleep(300.0) # Refresh sentiment every 5 minutes (300 seconds)

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

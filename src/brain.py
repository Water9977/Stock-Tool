import asyncio
import os
import yfinance as yf
from google import genai
from pydantic import BaseModel, Field
import aiohttp
import feedparser

class SentimentDecision(BaseModel):
    ticker: str
    sentiment_score: float = Field(..., description="Value between -1.0 (very negative) and 1.0 (very positive)")
    conviction_score: float = Field(..., description="Confidence index from 0.0 to 1.0")
    reasoning: str = Field(..., description="Short explanation of why the score was assigned")
    action_gate: str = Field(..., description="Must be one of: 'ALLOW_BUY', 'ALLOW_SELL', 'ALLOW_ALL', 'VETO', or 'FLATTEN'")

class BatchSentimentDecision(BaseModel):
    decisions: list[SentimentDecision] = Field(..., description="List of sentiment decisions for all stock tickers")

class BrainEngine:
    def __init__(self):
        self.watch_symbols = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "SBIN.NS", "AXISBANK.NS"
        ]
        self.gates = {s.replace(".NS", ""): "VETO" for s in self.watch_symbols}
        self.global_headlines = []
        self.headline_cache = {}  # Local cache to prevent redundant Gemini API calls
        
        # Initialize Gemini Client
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("WARNING: GEMINI_API_KEY not found in env. Cold Path AI analysis is disabled.")
            self.client = None
        else:
            self.client = genai.Client(api_key=api_key)

    async def run(self):
        """
        The COLD PATH (1-15s)
        Runs asynchronously every 300 seconds (5 minutes) to fetch news and query Gemini.
        Uses a local cache to skip Gemini calls if headlines have not changed.
        """
        print("Starting Cold Path AI Sentiment Engine...")
        if not self.client:
            # If AI is disabled, default gates to ALLOW_ALL so trading can proceed
            self.gates = {s.replace(".NS", ""): "ALLOW_ALL" for s in self.watch_symbols}
            return
            
        while True:
            symbol_news = {}
            for yf_symbol in self.watch_symbols:
                try:
                    # Fetch live news headlines from yfinance in executor thread
                    headlines = await asyncio.to_thread(self._fetch_news, yf_symbol)
                    symbol_news[yf_symbol] = headlines
                except Exception as e:
                    print(f"[COLD PATH ERROR] Failed fetching news for {yf_symbol}: {e}")
                    symbol_news[yf_symbol] = []
            
            # Construct a combined representation of news to check cache
            combined_news_str = ""
            for sym, headlines in symbol_news.items():
                combined_news_str += f"{sym}:{'|'.join(headlines)};"
            
            # Check cache to avoid redundant API requests
            if self.headline_cache.get("combined") == combined_news_str:
                print("[COLD PATH] Watchlist news unchanged. Skipping Gemini call.")
                await asyncio.sleep(300.0)
                continue

            # Build batch prompt
            prompt_lines = [
                "You are an intraday risk management AI. Analyze the following news for the stock symbols and generate an action gate for each.",
                f"GLOBAL MARKET MACRO NEWS:\n{chr(10).join(self.global_headlines) if self.global_headlines else 'None available'}\n"
            ]
            
            for sym, headlines in symbol_news.items():
                prompt_lines.append(f"STOCK-SPECIFIC HEADLINES for {sym}:")
                if headlines:
                    prompt_lines.append(chr(10).join(headlines))
                else:
                    prompt_lines.append("No recent specific headlines.")
                prompt_lines.append("")
                
            prompt_lines.append("""
For each ticker, output a SentimentDecision with the EXACT ticker name (e.g. 'RELIANCE.NS').
Rules:
1. If the news contains severe negative events (e.g. fraud, crash, bad earnings), set action_gate to 'ALLOW_SELL' or 'FLATTEN' (close all positions).
2. If the news contains major positive catalysts (e.g. big contract win, great earnings), set action_gate to 'ALLOW_BUY'.
3. If the news is routine, unrelated, or there are no headlines, set action_gate to 'ALLOW_ALL' so technical indicators can trade normally.
4. Output sentiment and conviction scores.
""")
            prompt = "\n".join(prompt_lines)
            
            # Retry loop with exponential backoff
            max_retries = 3
            backoff_base = 2
            api_call_success = False
            
            for attempt in range(max_retries):
                try:
                    # Define JSON schema manually as a dictionary to bypass the Pydantic nested $defs validation bug
                    schema = {
                        'type': 'OBJECT',
                        'properties': {
                            'decisions': {
                                'type': 'ARRAY',
                                'items': {
                                    'type': 'OBJECT',
                                    'properties': {
                                        'ticker': {'type': 'STRING'},
                                        'sentiment_score': {'type': 'NUMBER', 'description': 'Value between -1.0 and 1.0'},
                                        'conviction_score': {'type': 'NUMBER', 'description': '0.0 to 1.0'},
                                        'reasoning': {'type': 'STRING', 'description': 'Short explanation of why the score was assigned'},
                                        'action_gate': {'type': 'STRING', 'description': "Must be one of: 'ALLOW_BUY', 'ALLOW_SELL', 'ALLOW_ALL', 'VETO', or 'FLATTEN'"},
                                    },
                                    'required': ['ticker', 'sentiment_score', 'conviction_score', 'reasoning', 'action_gate'],
                                }
                            }
                        },
                        'required': ['decisions']
                    }
                    
                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model='gemini-3.1-flash-lite',
                        contents=prompt,
                        config={
                            'response_mime_type': 'application/json',
                            'response_schema': schema,
                        }
                    )
                    
                    import json
                    parsed_data = json.loads(response.text)
                    decisions = parsed_data.get("decisions", [])
                    
                    # Process and update decisions
                    updated_symbols = set()
                    for dec in decisions:
                        ticker = dec.get("ticker", "")
                        base_symbol = ticker.replace(".NS", "")
                        updated_symbols.add(base_symbol)
                        
                        action_gate = dec.get("action_gate", "ALLOW_ALL")
                        sentiment_score = dec.get("sentiment_score", 0.0)
                        reasoning = dec.get("reasoning", "")
                        
                        # Find the matching key in self.gates
                        for k in list(self.gates.keys()):
                            if base_symbol in k:
                                self.gates[k] = action_gate
                        
                        print(f"[COLD PATH] {ticker} -> Gate: {action_gate} | Score: {sentiment_score} | Reasoning: {reasoning}")
                    
                    # Default any tickers that were not returned in the API response to ALLOW_ALL
                    for yf_symbol in self.watch_symbols:
                        base_symbol = yf_symbol.replace(".NS", "")
                        if base_symbol not in updated_symbols:
                            self.gates[base_symbol] = "ALLOW_ALL"
                    
                    # Update cache
                    self.headline_cache["combined"] = combined_news_str
                    api_call_success = True
                    break
                except Exception as e:
                    print(f"[COLD PATH ERROR] Attempt {attempt+1} failed querying Gemini API: {e}")
                    if attempt < max_retries - 1:
                        sleep_time = backoff_base ** attempt
                        print(f"Retrying in {sleep_time} seconds...")
                        await asyncio.sleep(sleep_time)
                    else:
                        print("Max retries reached. Keeping previous gates.")
            
            await asyncio.sleep(300.0) # Refresh sentiment every 5 minutes (300 seconds)

    def _fetch_news(self, ticker: str):
        stock = yf.Ticker(ticker)
        news_items = stock.news
        if not news_items:
            return []
        # Return last 5 headlines
        return [item.get('title', '') for item in news_items[:5]]

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

    def get_gate_status(self, symbol: str) -> str:
        """Returns ALLOW_BUY, ALLOW_SELL, VETO, or FLATTEN."""
        for k in self.gates:
            if k in symbol:
                return self.gates[k]
        return "ALLOW_ALL" # Default safe state if not evaluated yet

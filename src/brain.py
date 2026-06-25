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

class BrainEngine:
    def __init__(self):
        self.watch_symbols = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "SBIN.NS", "AXISBANK.NS"
        ]
        self.gates = {s.replace(".NS", ""): "VETO" for s in self.watch_symbols}
        self.global_headlines = []
        
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
        Runs asynchronously every 30 seconds to fetch news and query Gemini.
        """
        print("Starting Cold Path AI Sentiment Engine...")
        if not self.client:
            return
            
        while True:
            for yf_symbol in self.watch_symbols:
                try:
                    # 1. Fetch live news headlines from yfinance
                    # Run the sync yfinance block in an executor thread to avoid blocking asyncio
                    headlines = await asyncio.to_thread(self._fetch_news, yf_symbol)
                    
                    if headlines:
                        # 2. Query Gemini 3.1 Flash-Lite
                        prompt = f"""You are an intraday risk management AI. Analyze the following news for {yf_symbol}.

GLOBAL MARKET MACRO NEWS:
{chr(10).join(self.global_headlines) if self.global_headlines else 'None available'}

STOCK-SPECIFIC HEADLINES for {yf_symbol}:
{chr(10).join(headlines) if headlines else 'No recent specific headlines.'}

1. If the news contains severe negative events (e.g. fraud, crash, bad earnings), set action_gate to 'ALLOW_SELL' or 'FLATTEN' (close all positions).
2. If the news contains major positive catalysts (e.g. big contract win, great earnings), set action_gate to 'ALLOW_BUY'.
3. If the news is routine, unrelated, or there are no headlines, set action_gate to 'ALLOW_ALL' so technical indicators can trade normally.
4. Output sentiment and conviction scores.
"""
                        
                        response = await asyncio.to_thread(
                            self.client.models.generate_content,
                            model='gemini-1.5-flash', # Note: Using flash currently available until gemini-3.1-flash-lite identifier is confirmed/updated via API
                            contents=prompt,
                            config={
                                'response_mime_type': 'application/json',
                                'response_schema': SentimentDecision,
                            }
                        )
                        
                        decision: SentimentDecision = response.parsed
                        base_symbol = yf_symbol.replace(".NS", "")
                        
                        # Strip NSE_EQ| prefix format for matching if needed in future
                        for k in list(self.gates.keys()):
                            if base_symbol in k:
                                self.gates[k] = decision.action_gate
                        
                        print(f"[COLD PATH] {yf_symbol} -> Gate: {decision.action_gate} | Score: {decision.sentiment_score} | Reasoning: {decision.reasoning}")
                except Exception as e:
                    print(f"[COLD PATH ERROR] Failed processing {yf_symbol}: {e}")
                    
            await asyncio.sleep(30.0) # Refresh sentiment every 30 seconds

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

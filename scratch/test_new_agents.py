import asyncio
import os
import json
import logging
import yfinance as yf
from datetime import datetime, timezone
import pandas as pd
from dotenv import load_dotenv

# Set up simple logging
logging.basicConfig(level=logging.INFO)

# Load env
load_dotenv()

from src.brain import EarningsAgent, FraudAgent

async def test_agents():
    # 1. Test EarningsAgent
    print("\n--- Testing EarningsAgent ---")
    watch_symbols = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "SBIN.NS", "AXISBANK.NS"]
    ea = EarningsAgent()
    pead_data = await asyncio.to_thread(ea.run, watch_symbols)
    print("EarningsAgent Result:", pead_data)

    # 2. Test FraudAgent
    print("\n--- Testing FraudAgent ---")
    fa = FraudAgent()
    # Inject some mock news data
    mock_news = {
        "RELIANCE.NS": ["Reliance announces new clean energy plan", "Jio IPO is delayed"],
        "TCS.NS": ["TCS wins $1B contract", "Company looks to expand"],
        "INFY.NS": ["SEBI launches investigation into Infosys accounting", "Infosys shares fall 5%"],
        "SBIN.NS": ["SBI reports record quarterly profit"],
        "AXISBANK.NS": ["Axis Bank auditor resigns abruptly amid fraud allegations"]
    }
    fraud_data = fa.run(mock_news)
    print("FraudAgent Result:", fraud_data)
    
if __name__ == "__main__":
    asyncio.run(test_agents())

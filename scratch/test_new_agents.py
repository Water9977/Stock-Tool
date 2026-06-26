import asyncio
import logging
from dotenv import load_dotenv

# Set up simple logging
logging.basicConfig(level=logging.INFO)

# Load env
load_dotenv()

from src.brain import FraudAgent

async def test_agents():
    # FraudAgent — deterministic India-tuned governance red-flag matcher
    print("\n--- Testing FraudAgent ---")
    fa = FraudAgent()
    mock_news = {
        "SBIN.NS": ["SBI reports record quarterly profit"],                                   # clean
        "ITC.NS": ["ITC announces new FMCG launch", "Promoter pledge raised to 40%"],         # promoter pledge
        "NTPC.NS": ["NTPC commissions new plant"],                                            # clean
        "ONGC.NS": ["SEBI penalty imposed on ONGC official for disclosure lapse"],            # sebi penalty
        "COALINDIA.NS": ["ED raid at Coal India offices over alleged irregularities"],        # ed raid
    }
    fraud_data = fa.run(mock_news)
    print("FraudAgent Result:", fraud_data)

    expected = {"SBIN.NS": False, "ITC.NS": True, "NTPC.NS": False, "ONGC.NS": True, "COALINDIA.NS": True}
    assert fraud_data == expected, f"MISMATCH: got {fraud_data}, expected {expected}"
    print("\nPASS: FraudAgent matched all expected flags.")

if __name__ == "__main__":
    asyncio.run(test_agents())

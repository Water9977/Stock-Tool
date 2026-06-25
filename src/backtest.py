import asyncio
import random
import sys
import os

# Add project root to path for absolute imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.technicals import TechnicalEngine

async def run_backtest():
    print("Starting Offline Backtest...")
    technicals = TechnicalEngine()
    tick_queue = asyncio.Queue()
    
    # Run technicals in background
    task = asyncio.create_task(technicals.run(tick_queue))
    
    symbol = "NSE_EQ|INE002A01018" # RELIANCE
    base_price = 2500.0
    
    print(f"Simulating 100 ticks for {symbol}...")
    
    for i in range(100):
        # We manipulate the price deterministically to test indicators
        if i < 40:
            base_price -= random.uniform(1.0, 5.0) # Downward to simulate oversold
        else:
            base_price += random.uniform(2.0, 6.0) # Upward to simulate overbought
            
        await tick_queue.put({"symbol": symbol, "price": base_price})
        
        # Give technical engine a moment to process
        await asyncio.sleep(0.01)
        
        signal = technicals.get_signal(symbol)
        if signal != "NEUTRAL":
            rsi_val = technicals.indicators[symbol]['rsi'][-1]
            print(f"Tick {i+1:03d}: Price Rs.{base_price:.2f} -> SIGNAL: {signal} (RSI: {rsi_val:.2f})")
            
    # Cleanup
    task.cancel()
    print("Backtest Complete.")

if __name__ == "__main__":
    asyncio.run(run_backtest())

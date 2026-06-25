import asyncio
import os
from src.config import Config
from src.db import init_db, start_day_stats, get_connection, get_realized_pnl
from src.auth import authenticate_upstox
from src.market_feed import MarketFeed
from src.orders import place_order

async def test_pipeline():
    print("=== STARTING STOCK TOOL PIPELINE TEST ===")
    
    # 1. Validate Config Loading
    print("Loading config...")
    assert Config.TRADING_MODE == "VIRTUAL", "Expected TRADING_MODE to be VIRTUAL"
    print("[OK] Configuration validated successfully.")

    # 2. Initialize Database
    print("Initializing SQLite Database...")
    init_db()
    start_day_stats(100000.0)
    print("[OK] Database initialized.")

    # 3. Test Authentication
    print("Testing authentication (Mock token generation)...")
    token = await authenticate_upstox()
    assert token == "mock_virtual_access_token_12345", f"Unexpected token: {token}"
    print(f"[OK] Authentication successful. Received token: {token}")

    # 4. Initialize Feed
    print("Starting simulated market feed...")
    feed = MarketFeed()
    await feed.start()
    await asyncio.sleep(1.0) # Wait for simulator to tick
    
    # Check simulated stock price
    reliance_price = feed.get_price("NSE_EQ|INE002A01018")
    print(f"[OK] Simulated price for RELIANCE (LTP): Rs. {reliance_price}")
    assert reliance_price > 0, "Price should be greater than 0"

    # 5. Place a simulated order
    print("Placing a simulated BUY order for 1 share of RELIANCE...")
    res = await place_order(
        symbol="NSE_EQ|INE002A01018",
        quantity=1,
        transaction_type="BUY",
        access_token=token,
        target_pct=0.02,
        sl_pct=0.01
    )
    
    print("Order response:", res)
    assert res["status"] == "FILLED", f"Unexpected status: {res.get('status')} - Reason: {res.get('reason')}"
    print(f"[OK] Order placed successfully! Order ID: {res['order_id']}")

    # 6. Verify trade logging in DB
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = ?", (res["order_id"],))
    db_row = cursor.fetchone()
    conn.close()
    
    assert db_row is not None, "Order should be saved in the database"
    print(f"[OK] Database trade log verified: {db_row}")
    
    # Clean up feed
    feed.stop()
    print("=== PIPELINE TEST COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(test_pipeline())

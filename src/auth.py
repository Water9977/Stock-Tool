import httpx
import json
import os
import asyncio
from src.config import Config

SESSION_FILE = ".session.json"

async def authenticate_upstox() -> str:
    """
    Obtains the daily access token.
    If TRADING_MODE is VIRTUAL, returns a mock token.
    If UPSTOX_ACCESS_TOKEN is set, uses it directly (skip OAuth browser dance).
    Otherwise executes manual OAuth login sequence to comply with SEBI regulations.
    """
    if Config.TRADING_MODE == "VIRTUAL":
        mock_session = {
            "access_token": "mock_virtual_access_token_12345",
            "extended_token": "mock_extended_token_abcde",
            "token_type": "Bearer"
        }
        with open(SESSION_FILE, "w") as f:
            json.dump(mock_session, f)
        print("Running in VIRTUAL mode. Mock token generated and saved.")
        return mock_session["access_token"]

    # Fast path: pre-set daily token (generated manually at account.upstox.com/developer/apps)
    preset_token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if preset_token:
        print("UPSTOX_ACCESS_TOKEN found in env. Skipping OAuth flow.")
        return preset_token

    # Check live credentials
    Config.validate()

    print("\n--- SEBI COMPLIANT MANUAL AUTHENTICATION ---")
    print("Running in LIVE mode. You must manually authorize the application.")
    
    url = (
        f"https://api.upstox.com/v2/login/authorization/dialog?"
        f"client_id={Config.CLIENT_ID}&redirect_uri={Config.REDIRECT_URI}&response_type=code"
    )
    
    print("\n1. Please open the following URL in your browser:")
    print(f"\n{url}\n")
    print("2. Log in and approve access.")
    print(f"3. You will be redirected to {Config.REDIRECT_URI} with a '?code=...' parameter.")
    
    auth_code = await asyncio.to_thread(input, "\nEnter the 'code' parameter from the URL: ")
    auth_code = auth_code.strip()
    
    if not auth_code:
        raise ValueError("Authorization code cannot be empty.")

    print("\nExchanging code for access token...")
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.upstox.com/v2/login/authorization/token", data={
            "code": auth_code,
            "client_id": Config.CLIENT_ID,
            "client_secret": Config.CLIENT_SECRET,
            "redirect_uri": Config.REDIRECT_URI,
            "grant_type": "authorization_code"
        }, headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        })
        
        if r.status_code != 200:
            raise RuntimeError(f"Failed to exchange auth code for access token. Response: {r.text}")

        data = r.json()
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
        
        print("LIVE authentication successful. Token saved.\n")
        return data["access_token"]

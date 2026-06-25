import httpx
import pyotp
import json
import os
from urllib.parse import parse_qs, urlparse
from src.config import Config

SESSION_FILE = ".session.json"

def get_totp() -> str:
    """Generates the current 6-digit TOTP code using the shared secret key."""
    if not Config.TOTP_SECRET:
        return "000000"
    totp = pyotp.TOTP(Config.TOTP_SECRET)
    return totp.now()

async def authenticate_upstox() -> str:
    """
    Obtains the daily access token.
    If TRADING_MODE is VIRTUAL, returns a mock token.
    If TRADING_MODE is LIVE, executes the automated HTTP/2FA login sequence.
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

    # Check live credentials
    Config.validate()

    print("Running in LIVE mode. Starting programmatic 2FA authentication...")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        # 1. Initiate login dialog
        url = (
            f"https://api.upstox.com/v2/login/authorization/dialog?"
            f"client_id={Config.CLIENT_ID}&redirect_uri={Config.REDIRECT_URI}&response_type=code"
        )
        r1 = await client.get(url)
        if r1.status_code != 200:
            raise RuntimeError(f"Failed to initiate Upstox auth page. HTTP Status: {r1.status_code}")

        # 2. Request OTP sending mobile number
        r2 = await client.post("https://api.upstox.com/v2/login/send-otp", json={
            "mobile_number": Config.MOBILE_NUMBER
        })
        if r2.status_code != 200:
            raise RuntimeError(f"Failed to send mobile login request. Response: {r2.text}")

        # 3. Submit generated 2FA TOTP code
        totp_code = get_totp()
        r3 = await client.post("https://api.upstox.com/v2/login/verify-otp", json={
            "otp": totp_code
        })
        if r3.status_code != 200:
            raise RuntimeError(f"Failed to verify TOTP code. Response: {r3.text}")

        # 4. Verify 6-digit user PIN
        r4 = await client.post("https://api.upstox.com/v2/login/verify-pin", json={
            "pin": Config.PIN
        })
        if r4.status_code != 200:
            raise RuntimeError(f"Failed to verify PIN. Response: {r4.text}")

        # 5. Access the authorization redirect url to extract auth code
        r5 = await client.get(url)
        redirect_url = r5.headers.get("Location")
        if not redirect_url:
            raise RuntimeError(f"Failed to retrieve redirect location header from Upstox.")
        
        parsed = urlparse(redirect_url)
        query = parse_qs(parsed.query)
        if "code" not in query:
            raise RuntimeError(f"Authorization code missing from redirect URL: {redirect_url}")
        auth_code = query["code"][0]

        # 6. Exchange code for access token
        r6 = await client.post("https://api.upstox.com/v2/oauth/token", data={
            "code": auth_code,
            "client_id": Config.CLIENT_ID,
            "client_secret": Config.CLIENT_SECRET,
            "redirect_uri": Config.REDIRECT_URI,
            "grant_type": "authorization_code"
        })
        if r6.status_code != 200:
            raise RuntimeError(f"Failed to exchange auth code for access token. Response: {r6.text}")

        data = r6.json()
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
        
        print("LIVE authentication successful. Token saved.")
        return data["access_token"]

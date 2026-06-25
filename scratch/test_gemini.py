import os
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()

class SentimentDecision(BaseModel):
    ticker: str
    sentiment_score: float = Field(..., description="Value between -1.0 and 1.0")
    conviction_score: float = Field(..., description="0.0 to 1.0")
    reasoning: str
    action_gate: str

def test_gemini():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set in the environment variables.")
        print("Please add it to your .env file: GEMINI_API_KEY=your_key_here")
        return
        
    print(f"Key found: {api_key[:6]}...{api_key[-4:] if len(api_key) > 10 else ''}")
    print("Initializing GenAI Client...")
    try:
        client = genai.Client(api_key=api_key)
        print("Sending test prompt to gemini-3.1-flash-lite...")
        
        # Test basic text generation
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents="Hello! Let me know if you can read this.",
        )
        print(f"Response: {response.text}")
        
        # Test structured output JSON schema
        print("Testing structured JSON output schema...")
        prompt = "Analyze news for SBIN: SBI Q4 profit jumps 24% to Rs 20,698 crore, beats estimates."
        response_schema = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'response_schema': SentimentDecision,
            }
        )
        print("Parsed JSON response:")
        print(response_schema.text)
        print("[SUCCESS] Gemini API setup is 100% correct!")
    except Exception as e:
        print(f"[FAILED] Gemini API call failed: {e}")

if __name__ == "__main__":
    test_gemini()

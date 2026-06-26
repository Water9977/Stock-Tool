# Analysis: $3000 AI Trading Bot Experiment (Kirat)

**Video Link:** [https://ai-trading.100xdevs.com/](https://ai-trading.100xdevs.com/)
**Context:** Kirat built an AI trading bot from scratch and let it trade with $3000 overnight.

## Key Architectural Takeaways

### 1. LLM Context Injection (Portfolio & Technicals)
Instead of just asking the LLM to analyze a chart, Kirat provided full spatial awareness to the agent by injecting exact state data into the prompt:
- **Financial State:** Available cash, total portfolio value, and all currently open positions with unrealized P&L.
- **Technical State:** The backend fetched the last 50 candles, calculated Mid Prices, EMA 20, and MACD, and sliced them down to the **last 10 values**. These values were converted into comma-separated strings and injected into the prompt for both 5-minute and 4-hour timeframes.

### 2. Decision Execution via Tool Calling
Rather than using generic JSON output parsing, the agent was provided with specific tools using the Vercel AI SDK:
- `create_position(symbol, side, quantity)`
- `close_all_positions()`
This allowed the LLM to autonomously execute trades and manage risk directly.

### 3. Multi-Model Portfolio Strategy
Kirat divided the $3000 capital into three $1000 sub-accounts and ran three different models simultaneously via OpenRouter:
- Claude 3.5 Sonnet
- Qwen (Largest model)
- DeepSeek R1
*Result:* Claude 3.5 Sonnet and DeepSeek R1 performed best overnight (+$40 to +$67), successfully capturing a large leveraged long position on Solana. Qwen remained mostly flat, despite performing better in previous backtesting ("Alpha Arena").

### 4. Execution Frequency
The system was invoked exactly **once every 5 minutes** via a `setInterval` cron job. This controlled API costs (pre-loaded with $20) while remaining responsive enough to capture intraday trends.

### 5. AI-Driven Risk Management
In a highly aggressive move, the backend contained **no hardcoded stop-losses** or risk guardrails, despite the bots using 15x to 20x leverage. The LLMs were entirely responsible for deciding when to invoke `close_all_positions()`.

---

## Implications for Stock-Tool Architecture

Based on these findings, we can significantly improve our `Stock-Tool` by adopting the Context Injection strategy.

**Our Current State:**
- `NewsAgent`, `SentimentAnalyst`, and `MacroAnalyst` provide qualitative insights.
- `RiskGatekeeper` decides whether to `ALLOW_BUY`, `ALLOW_SELL`, `VETO`, or `FLATTEN` primarily based on sentiment and macro data.
- The `TechnicalEngine` operates separately, generating `BUY` or `SELL` signals based on RSI and MACD.

**Proposed Integration:**
We will inject the following quantitative data into the `RiskGatekeeper`'s prompt:
1. **Financial State:** Current available capital (`MAX_CAPITAL_PER_TRADE` vs actual available balance) and current open virtual positions.
2. **Technical Trends:** The last 10 RSI and MACD values for the symbol being evaluated.

By providing the `RiskGatekeeper` with recent indicator trends and portfolio health, it can make a far more informed decision on whether to align with the `TechnicalEngine`'s trigger or veto it due to overexposure or conflicting qualitative news.

# Antigravity / Gemini Agent Instructions

This file defines the core behavioral rules and instructions for the agent working in this repository.

---

## 1. Skill Utilization & Transparency

> **THIS IS THE HIGHEST PRIORITY RULE. NO EXCEPTIONS.**

*   **MANDATORY SKILL REVIEW:** At the start of EVERY session and EVERY task, you MUST review the available skills list and identify which ones apply to the current work.
*   **PROACTIVE SKILL USAGE:** You MUST use the most appropriate skill(s) for the task at hand. Using no skill when one exists is a VIOLATION.
*   **MANDATORY EXPLICIT DISCLOSURE:** **WHENEVER A SKILL IS USED (OR CONSIDERED AND REJECTED), YOU MUST STATE THIS IN BOLD AT THE BEGINNING OF YOUR RESPONSE.** Examples:
    *   ✅ **"I am using the `gemini-api-dev` skill for the Gemini API integration."**
    *   ✅ **"I am using the `smart-git-automation` skill for this commit."**
    *   ✅ **"I considered `debug` skill but this is a simple fix — proceeding directly."**
*   **FAILURE TO EXPLICITLY DECLARE SKILLS IS A PROTOCOL VIOLATION AND MUST NOT OCCUR.**

### Skills Most Relevant to This Repository (always consider these)
| Skill | When to Use |
|-------|-------------|
| `gemini-api-dev` | Any Gemini API calls, prompt construction, structured output |
| `smart-git-automation` | All commits, pushes, branch operations |
| `debugger` | Any bug, unexpected behavior, test failure |
| `powershell-windows` | Any terminal commands (this is a Windows/PowerShell environment) |
| `gemini-api-dev` | API integrations — Upstox REST/WebSocket, Yahoo Finance |

---

## 2. Validation Over Assumption

> **ZERO ASSUMPTION POLICY — NO EXCEPTIONS.**

*   **NEVER assume** technical facts, API endpoint paths, payload shapes, or library behavior.
*   **NEVER write code** against an API without first validating the schema via `search_web`, `firecrawl`, or by inspecting the installed package directly (e.g., `python -c "import module; print(...)"`)
*   **MANDATORY VALIDATION:** If ANY doubt exists about:
    *   A broker API endpoint structure (Upstox, etc.)
    *   A library's data output format (yfinance, talipp, protobuf field names)
    *   Market rules or exchange timings
    *   → You MUST validate using `search_web` or by running an inspection command **before** writing code.

---

## 3. Repository-Specific Rules

*   **Architecture Document:** After any significant change to the system design, update `architecture.md` immediately.
*   **GitHub Push:** Every session that produces working code changes MUST end with `git add`, `git commit`, and `git push` to the `master` branch.
*   **Proto Field Names:** Upstox V3 proto fields use **camelCase** in Python:
    *   `feed.fullFeed` (not `full_feed`)
    *   `fullFeed.marketFF.ltpc.ltp` (for equity full-mode subscriptions)
    *   `feed.ltpc.ltp` (for LTPC-mode subscriptions)
*   **Windows PowerShell:** NEVER use `&&` to chain commands. Use `;` instead.
*   **Python Path:** Python is at `C:\Users\sidds\AppData\Local\Programs\Python\Python313\python.exe`. Always use `python -m pip` not plain `pip`.
*   **Singleton Pattern:** `MarketFeed` and `BrainEngine` are singletons. Always call `.set_access_token()` on MarketFeed before `.start()`.

---

## 4. Communication

*   Keep responses concise and focused on immediate implementation.
*   Use tables and structured formats for complex concepts.
*   Always link to modified files using `file://` markdown links in responses.
*   End every session by listing: **What was built**, **What was tested**, **What remains**.

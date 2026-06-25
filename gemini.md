# Antigravity / Gemini Agent Instructions

This file defines the core behavioral rules and instructions for the agent working in this repository.

## 1. Skill Utilization & Transparency
*   **PROACTIVE SKILL USAGE:** Always review the available skills and utilize the most appropriate ones for the current task.
*   **MANDATORY EXPLICIT DISCLOSURE:** **WHENEVER A SPECIFIC SKILL IS USED TO SOLVE A PROBLEM OR RETRIEVE INFORMATION, YOU MUST EXPLICITLY STATE THIS IN BOLD AT THE BEGINNING OF YOUR RESPONSE TO THE USER.** For example: **"I am using the `gemini-api-dev` skill to..."**. **FAILURE TO EXPLICITLY DECLARE THE USED SKILLS IS A VIOLATION OF PROTOCOL.**

## 2. Validation Over Assumption
*   **Zero Assumption Policy:** Never assume technical facts, API endpoints, or market rules.
*   **Mandatory Search:** If there is any doubt about how a library works, how a broker API expects a payload, or what the best practice is, you MUST validate it using `search_web` or the `firecrawlMCP` server.

## 3. Communication
*   Keep responses simple, concise, and focused on the immediate implementation.
*   Use tables and structured formats to explain complex concepts when discussing strategy or research.

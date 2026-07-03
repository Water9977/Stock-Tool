# Stock Agent — Full System Audit & Remediation Plan

**Auditor:** Claude Fable 5
**Date:** 2026-07-03
**Scope:** Every source file, line by line. Hot / Warm / Cold paths, DB, brain, deployment, security.
**Method:** Static read of all code + live verification against Upstox docs, Gemini docs, and the *actually installed* `yfinance` / `talipp` packages in `venv`.
**Rule followed:** No code was changed. This is a plan only.

**Key context from the owner (drives severity):**
- End goal is **real-money LIVE trading** on Upstox. Therefore every live-order-path defect is treated as a **P0 blocker**, not a nice-to-have.
- Brain redesign should cover **both** a standalone Agent Skill and an in-process plugin module, with a recommendation.
- gstack integration: **high-level only**.

---

## 0. TL;DR — The Five Things That Matter Most

1. **Live order placement is broken at the parsing layer.** Upstox V3 returns `data.order_ids` (a list). The code reads `data.order_id` (singular). Verified against Upstox live docs. Every real order will be seen as FAILED *even though it actually hit the exchange* → untracked, unmanaged real positions. **P0.**
2. **The entire news + fraud pipeline is silently dead.** `yfinance` (installed v1.4.1) moved the headline to `item['content']['title']`. The code reads `item['title']`, which now returns `None`. Verified live. FraudAgent never sees text, the AI always gets "No news". This is why every log line says *"No specific news identified"*. **P0 for the Cold Path's usefulness.**
3. **Trading endpoints are unauthenticated.** `/execute` places orders and `/admin/reset-simulation` wipes the DB with no auth. On a public Railway/Render URL wired to a real account, this is remote unauthorized trading. **P0.**
4. **Static IP is not wired.** Upstox + SEBI mandate a whitelisted static IP for order APIs (error `UDAPI1154`). Railway/Render rotate outbound IPs. Confirmed against SEBI Feb-4-2025 circular and Upstox docs. Live orders will be blocked until a static-IP proxy is in place. **P0 for live.**
5. **No order-status reconciliation.** After "submit", nothing checks whether the order filled, partially filled, was rejected, or at what price. SL/TP is computed off the pre-trade cache price, not the real fill. Phantom positions accumulate. **P0 for live.**

Everything else is below, ranked.

---

## 1. System Process Map (as it actually runs today)

### Startup (`src/main.py` `startup_event`)
```
init_db() → optional RESET_ON_START wipe → start_day_stats(STARTING_CAPITAL)
→ risk_engine.load_from_db() (restore loss streak)
→ authenticate_upstox()  (VIRTUAL: mock token | PAPER/LIVE: env token or manual OAuth)
→ MarketFeed().set_access_token().start()
→ sync_open_positions()  (rehydrate hot cache from SQLite)
→ TechnicalEngine(), BrainEngine()
→ create tasks: monitor_open_positions, automated_execution_loop, shutdown_scheduler
```

### 🟥 HOT PATH — price cache + exits
- `MarketFeed` (singleton). VIRTUAL = random walk each 1s. LIVE/PAPER = Upstox V3 WebSocket (Protobuf), falls back to REST LTP poll (1s) if no WS ticks in 30s.
- `_process_tick` updates `price_cache`, stamps `last_tick_time`, aggregates into 5-min OHLC candles, and **only broadcasts the previous candle's close when a new 5-min bucket opens**.
- `monitor_open_positions` loops every 0.1s: for each open position, compares live price to target/stop, fires exit, writes to DB. Skips work when `is_feed_healthy()` is False.

### 🟨 WARM PATH — indicators (`src/technicals.py`)
- Consumes the broadcast (one value per 5-min candle) into `talipp` RSI(14) + MACD(12,26,9).
- Emits BUY/SELL/NEUTRAL per symbol. Threshold: `RSI<40 OR (MACD bullish AND RSI<50)` → BUY (mirror for SELL).

### 🟦 COLD PATH — AI gate (`src/brain.py`)
- Every 5 min during market hours: fetch yfinance news (40 symbols, parallel) → deterministic `FraudAgent` → gather trailing 10 RSI/MACD + balance/positions → **one** `CommitteeAgent` Gemini call (`gemini-3.1-flash-lite`) → per-symbol gate (`ALLOW_BUY/SELL/ALL/VETO/FLATTEN`) + reasoning.
- Separate task polls Livemint markets RSS every 5 min for macro headlines.

### Decision join (`automated_execution_loop`, every 1s)
```
trading_allowed? → is_market_hours? → is_feed_healthy?
for each symbol:
  gate = brain.gates[base]            (FLATTEN → emergency exit, skip)
  signal = technicals.get_signal()    (NEUTRAL → skip)
  2-min per-symbol cooldown
  qty = min(MAX_CAPITAL_PER_TRADE, balance) / price
  BUY if signal==BUY and gate in {ALLOW_BUY, ALLOW_ALL}
  SELL if signal==SELL and gate in {ALLOW_SELL, ALLOW_ALL}
```

### Shutdown
- VIRTUAL: after `VIRTUAL_RUNTIME_HOURS` → flatten all + suspend (permanent until redeploy).
- PAPER/LIVE: at 15:25 IST → flatten all + suspend.

---

## 2. Findings — Hot Path & Live Order Correctness (real money)

### H-1 (P0) — Live order response parsed with the wrong key
**File:** [src/orders.py:125](src/orders.py:125)
**What:** `if "data" not in res or "order_id" not in res["data"]:` then `res["data"]["order_id"]`.
**Reality (verified on Upstox V3 docs):** success body is
```json
{ "status":"success", "data": { "order_ids": ["1644490272000"] }, "metadata": {"latency":30} }
```
The key is **`order_ids`** (plural, an array). `order_id` never exists.
**Impact:** Every live order is classified FAILED even though the exchange accepted it. The system never records the position, never monitors SL/TP, and may retry — leaving a **naked, unmanaged real-money position**.
**Fix steps:**
1. Read `res["data"]["order_ids"][0]` (guard empty list).
2. Treat HTTP 200 + presence of `order_ids` as accepted-by-broker, **not** as filled.
3. Handle the documented `slice` case where multiple `order_ids` come back (track all).

### H-2 (P0) — Exit orders hit the same parsing bug → double-exit loop
**File:** [src/orders.py:283](src/orders.py:283) and [src/orders.py:357](src/orders.py:357)
**What:** `monitor_open_positions` and `flatten_positions_for_symbol` call `execute_live_order`, then check `res["status"] != "SUBMITTED"`. Because of H-1 that check is always true for real orders.
**Impact:** The exit order *does* fire on the exchange (position closes), but the code thinks it failed, so it `continue`s **without** removing the position from `_open_positions`. Next 0.1s tick it tries to close again → **repeated opposite-side market orders → an unintended naked short** and an exit loop. Catastrophic for real capital.
**Fix steps:** Fix H-1 first. Then only remove/keep a position based on a *reconciled* order status (see H-4), not on the submit response alone.

### H-3 (P0) — Entry/exit price recorded is the pre-trade cache price, not the fill
**File:** [src/orders.py:187](src/orders.py:187) (LIVE branch stores `current_price`)
**What:** Market orders fill at an unknown price; the code stores the last cached LTP as the entry and derives target/stop from it.
**Impact:** SL/TP levels are anchored to a price the trade never got. Real P&L and risk are wrong from the first tick.
**Fix steps:** After acceptance, poll the order/trade book for the **average fill price** and quantity, then compute target/stop from that. Store broker fill price, not cache price.

### H-4 (P0) — No order-status reconciliation
**Files:** whole live path in [src/orders.py](src/orders.py)
**What:** Status is written as `LIVE_SUBMITTED` and never advanced. Nothing polls Order Details / Order Book / trade updates.
**Impact:** Rejected, cancelled, or partially filled orders live on as phantom positions in the DB and hot cache; the monitor "manages" positions that don't exist at the broker. Partial fills mean quantity mismatch.
**Fix steps:**
1. Add a reconciler task (poll `GET /v2/order/details` or subscribe to the order-updates WebSocket) that maps broker states → local states.
2. Only add to `_open_positions` once **filled**; use filled qty.
3. Add `LIVE_REJECTED` / `LIVE_PARTIAL` states and stop monitoring rejected orders.

### H-5 (P0 for live) — No global orders-per-second throttle → SEBI + Upstox rate breach
**File:** [src/main.py:51](src/main.py:51) loop over up to 40 symbols
**What:** Verified: SEBI Feb-4-2025 circular treats <10 OPS as "regular"; Upstox order APIs cap 50/sec, 500/min, 2000/30min. The 1s loop can fire many symbols in the same tick. Per-symbol 2-min cooldown doesn't bound a simultaneous burst across 40 symbols.
**Impact:** A synchronized signal across many symbols could exceed 10 OPS → rejects (`UDAPI` rate errors) and a SEBI compliance breach for a retail API user.
**Fix steps:** Add a global token-bucket limiter on order submission (target ≤ 8–9 OPS to stay under the retail threshold). Queue rather than drop.

### H-6 (P0 for live) — Static IP not wired for deployment
**Files:** [architecture.md](architecture.md) mentions it; nothing implements it. [Dockerfile](Dockerfile), [railway.toml](railway.toml), [render.yaml](render.yaml).
**What:** Verified against Upstox docs (`UDAPI1154`) and SEBI circular: order/modify/cancel must originate from a **registered static IP**.
**Impact:** On Railway/Render rotating IPs, every live order is blocked.
**Fix steps:** Route order traffic through a static-IP egress (Railway Static IP add-on, QuotaGuard, or a small fixed-IP VPS), and register that IP in the Upstox developer console. Document the exact IP-whitelist step in the runbook.

### H-7 (P1) — `is_feed_healthy()` mutates state as a side effect and is called from two tasks
**File:** [src/market_feed.py:407](src/market_feed.py:407)
**What:** The "getter" writes `self._feed_recovered_at`. Both `automated_execution_loop` and `monitor_open_positions` call it. Two callers race the recovery timer.
**Impact:** The 8-second recovery grace can be reset or short-circuited depending on interleaving, so trading may resume too early after a stale spell (the exact gap-jump risk this code was meant to prevent).
**Fix steps:** Make health evaluation pure; update `_feed_recovered_at` in one place (the tick handler). Compute health from timestamps only.

### H-8 (P2) — Emergency FLATTEN is blocked by the feed-health gate
**File:** [src/main.py:65](src/main.py:65) gate sits before the symbol loop, so FLATTEN can't run on a stale feed.
**What/Impact:** During a stale feed you can neither open nor emergency-exit. That's a deliberate trade-off (don't act on a frozen price), but for a real *"fraud detected, get out"* FLATTEN it may be the wrong call. Decide explicitly.
**Fix steps:** Consider allowing FLATTEN to route a market exit even on a stale feed (with a clear log), or document that FLATTEN waits for feed recovery.

### H-9 (P2) — REST poll uses deprecated v2 market-quote
**File:** [src/market_feed.py:292](src/market_feed.py:292)
**What:** Verified: v2 `market-quote/ltp` still works but is marked deprecated in favor of v3. The response's `instrument_token` field *does* return the full key (`NSE_EQ|INE...`), so the current mapping back into `price_cache` is **correct** — no bug there, but plan the v3 migration.

---

## 3. Findings — Warm Path (indicators)

### W-1 (P1) — `None` RSI can crash the warm-path task, killing signals for the day
**File:** [src/technicals.py:38](src/technicals.py:38)
**What (verified with installed talipp 2.7.0):** `len(RSI(14))` counts input count *including* `None` warmup padding. After 14 inputs the list length is 14 but the values are still `None` (first real value appears around the 15th input). The guard `len(rsi) >= 14` passes while `rsi[-1]` can still be `None`. Then `current_rsi < 40.0` does `None < 40.0` → **TypeError**.
**Impact:** `technicals.run` has no try/except; one TypeError kills the loop → no more indicator updates or signals for the rest of the session, silently.
**Fix steps:**
1. Gate on the value, not the length: `if rsi[-1] is not None and macd[-1] is not None and macd[-1].macd is not None`.
2. Wrap the `run` loop body in try/except that logs and continues.

### W-2 (P2) — 5-minute-only broadcast means ~2 hours of warmup daily
**File:** [src/market_feed.py:154](src/market_feed.py:154)
**What:** Only the closed candle's price reaches the warm path. RSI(14) needs 14 candles, MACD(26) needs 26 → ~130 min before the first MACD-backed signal each day, and the same delay in VIRTUAL demos.
**Impact:** Strategy is dark for the first ~2 hours; short sessions may never trade. Not a bug, but a real usability/latency constraint the docs undersell.
**Fix steps:** Consider seeding indicators at startup from historical candles (Upstox historical-candle API) so signals are live from the open. Or run a shorter candle for the warm path than the 5-min display candle.

### W-3 (P2) — Docs vs code threshold drift
[architecture.md](architecture.md) says "RSI<35 BUY / RSI>65 SELL"; code uses 40/60 + MACD tiebreak. Align docs to code.

---

## 4. Findings — Cold Path / Brain

### B-1 (P0 functional) — NewsAgent reads a field that no longer exists → whole news+fraud path dead
**File:** [src/brain.py:105](src/brain.py:105) `item.get('title','')`
**What (verified live against installed yfinance 1.4.1):** A news item's top-level keys are now `['id', 'content']`. `item.get('title')` → `None`. The title lives at `item['content']['title']`.
**Impact:** `fetch_yf_news` returns a list of empty strings; FraudAgent scans nothing (so **fraud is never detected**); the committee prompt shows "No news" for every symbol. The FLATTEN-on-fraud safety switch can therefore **never trigger from news**. This is the root cause of the all-`VETO`/all-"No specific news" logs.
**Fix steps:**
1. Read `item.get('content', {}).get('title')`, and also pull `content.summary`/`content.pubDate` for richer context.
2. Add a startup self-test that asserts at least one non-empty headline for a liquid symbol, and log LOUDLY if the schema breaks again.
3. **Pin `yfinance`** (see D-3) so a silent upstream schema change can't re-break this.

### B-2 (P1) — 429 retry path loops forever without counting attempts
**File:** [src/brain.py:80](src/brain.py:80)
**What:** On a 429/quota error it sleeps 65s and `continue`s **without** incrementing `attempt`. `max_retries` never bounds this branch.
**Impact:** A persistently exhausted quota pins the Cold Path in a 65s sleep loop indefinitely; gates go stale.
**Fix steps:** Count the attempt (or cap total 429 waits); after N, return `None` and let the caller fall back to the safe default.

### B-3 (P1) — Hard-timeout leaves orphan threads that can starve a 2-worker pool
**File:** [src/brain.py:19](src/brain.py:19) (`max_workers=2`) + [src/brain.py:21](src/brain.py:21)
**What:** A timed-out Gemini call keeps running in the pool (by design) for up to the full 180s. Two overlapping orphans occupy both workers; the next `submit().result()` blocks until one frees.
**Impact:** Repeated timeouts can wedge the Cold Path.
**Fix steps:** Raise `max_workers`, shorten `call_timeout` (180s is very long for flash-lite), and/or use the SDK's async client with a real HTTP timeout if/when available. Track in-flight count and skip a cycle rather than pile up.

### B-4 (P2) — Shared-state races on `_open_positions`
**Files:** [src/brain.py:298](src/brain.py:298) reads `_open_positions.values()` in async context; [src/orders.py:92](src/orders.py:92) sums `.values()` inside `to_thread`; monitor mutates the dict.
**What:** A plain dict is iterated in worker threads / async tasks while the event loop mutates it. Python can raise `dict changed size during iteration`, and balance math can read a half-updated view.
**Fix steps:** Guard `_open_positions` with a lock, or snapshot (`list(...)`) before iterating everywhere, or move to an async-safe structure. Centralize all mutations behind one module API.

### B-5 (P2) — Blocking DB/sync work on the event loop inside the Cold Path
**File:** [src/brain.py:297](src/brain.py:297) calls `get_available_balance()` (sync SQLite) directly in the async `run`.
**Fix steps:** Wrap in `asyncio.to_thread`, consistent with the rest of the codebase.

### B-6 (concept) — The AI gate adds little signal while news is dead
With B-1 unfixed, the committee has only technicals + empty news, and the prompt says "default to ALLOW_ALL". The gate is effectively noise or a rubber stamp. Fixing B-1 is a prerequisite for the brain being meaningful. See §8 for the redesign.

---

## 5. Findings — Database & State

### D-1 (P1) — Midnight IST rollover corrupts daily stats mid-session
**File:** [src/db.py](src/db.py) — everything keyed by `date.today()`.
**What:** VIRTUAL runtime is 8h and the loop can cross local midnight. After rollover, `get_realized_pnl` finds no row → returns 0 (loss breaker silently resets), and `get_starting_capital` returns the hardcoded `2000.0` fallback instead of the configured `300000`.
**Impact:** The daily-loss circuit breaker resets mid-run; wallet/balance math breaks after midnight; a fresh day's row is never created until the next restart.
**Fix steps:** Define the "trading day" explicitly (IST, with a fixed session boundary), create the day row at session start, and make fallbacks read Config, not a literal `2000.0`.

### D-2 (P2) — Inconsistent default capital literal
`start_day_stats(... = 2000.0)` and `get_starting_capital()` fallback `2000.0` contradict `Config.STARTING_CAPITAL` (300000) and the memory note. Route all defaults through Config.

### D-3 (P1) — Unpinned dependencies caused B-1 and will cause the next silent break
**File:** [requirements.txt](requirements.txt) — every package is unpinned.
**What:** The dead-news bug is exactly what floating `yfinance` produces.
**Fix steps:** Pin exact versions (`yfinance==1.4.1`, `talipp==2.7.0`, `google-genai==2.10.0`, `websockets==16.0`, `httpx==0.28.1`, `feedparser==6.0.12`, plus fastapi/uvicorn/pydantic). Add a periodic dependency-review, not blind upgrades.

---

## 6. Findings — Security

### S-1 (P0) — `/execute` places orders with no authentication
**File:** [src/main.py:256](src/main.py:256)
**What:** Anyone who can reach the URL can POST an arbitrary symbol/qty/BUY-SELL and place a real order, bypassing the brain gate and technical signal entirely.
**Fix steps:** Require an auth token/secret on all mutating endpoints; bind the dashboard behind auth; disable `/execute` in LIVE unless explicitly enabled.

### S-2 (P0) — `/admin/reset-simulation` wipes the DB unauthenticated
**File:** [src/main.py:308](src/main.py:308) — public DB wipe + position clear.
**Fix steps:** Auth-gate; in LIVE, refuse entirely or require a confirmation secret.

### S-3 (P1) — No auth/CORS on the dashboard data endpoints either
`/api/status`, `/portfolio`, `/prices` expose account balance, positions, and P&L publicly. Put the whole app behind auth before any public deploy.

### S-4 (P2) — Reflected/stored XSS in the dashboard
**File:** [src/static/index.html:1079](src/static/index.html:1079) (news `${title}`), plus watchlist/reason cells built via `innerHTML`.
**What:** External RSS/news titles are injected as HTML without escaping.
**Fix steps:** Escape all external text (use `textContent` or an escape helper) before insertion.

### S-5 (P2) — Live secrets on disk
`.env` holds real client id/secret, a ~313-char access token, and the Gemini key. Correctly gitignored (verified: only `.env.example` is tracked; `*.log`, `trading.db`, `.session.json` are ignored). Keep it that way; for cloud, use the platform secret store, never bake into the image. `.session.json` writes the token to disk in PAPER/LIVE — ensure the deploy disk isn't world-readable.

---

## 7. Findings — Backtest, Deployment, Misc

- **BT-1 (P1) Two diverging strategy copies.** [src/backtest.py](src/backtest.py) and the `/backtest` endpoint in [src/main.py:323](src/main.py:323) reimplement the same replay logic. They will drift. Extract one shared function.
- **BT-2 (P1) Backtest ignores the AI gate.** It runs technicals-only, so results **overstate** what the live system (technicals AND gate) would do. Either apply the gate in backtest or clearly label results as "technical-only upper bound".
- **BT-3 (P2) Fixed `qty=10` and same-bar entry+exit** allow slightly unrealistic fills (enter and hit target within one bar). Add position sizing consistent with live and bar-ordering realism.
- **DEP-1 (P2)** `@app.on_event("startup")` is deprecated in current FastAPI; migrate to the lifespan handler.
- **DEP-2 (P2)** VIRTUAL `shutdown_scheduler` permanently suspends after N hours with no auto-restart — conflicts with the stated 24/7 goal. Decide: rolling daily reset vs. permanent stop.
- **CFG-1 (P1) Risk config is very loose and docs are stale.** `.env`: capital 300000, `MAX_DAILY_LOSS=200000` (**66% drawdown allowed**), `MAX_CAPITAL_PER_TRADE=150000` (**half the account in one trade**). [architecture.md](architecture.md) still describes a Rs.700/Rs.2000 account. For real money, re-derive these from a defined per-trade risk % and reconcile the docs. (Consistent with the saved note on watchlist/capital constraints.)
- **CFG-2 (P2)** `pyotp` is in requirements but auth is manual now — unused dependency.
- **T-1 (P2)** [verify_setup.py](verify_setup.py) tests `BANKBARODA` (`NSE_EQ|INE028A01039`), which is **not** in `SYMBOL_MAP`, so it isn't in `price_cache`; `get_price` returns 0.0 and the `> 0` assert fails. The smoke test is broken. Point it at a watchlist symbol.
- **T-2 (P1)** No tests cover the live order path, reconciliation, or the brain. Add them before going live.
- **OPS-1 (P2)** Cooldown timestamp is set even when `place_order` returns REJECTED ([src/main.py:117](src/main.py:117)), so a rejected order blocks retries for 2 min. Set cooldown only on accepted orders.

---

## 8. Brain Deep Dive — Current State & Proposed Redesign

### 8.1 What's wrong conceptually today
1. **Dead inputs (B-1):** news/fraud contribute nothing; the "committee" is technicals + empty text.
2. **One monolithic prompt** plays Sentiment + Macro + Risk. No separation, no per-role validation, hard to test, and a single JSON parse failure drops all 40 gates to VETO.
3. **No memory / no evaluation:** gates aren't scored against outcomes; there's no feedback loop, so the AI can't get better or be trusted.
4. **Coupling:** the brain lives inside the trading server's event loop, shares mutable globals (`_open_positions`), and blocks the loop with sync calls. You can't run/test it standalone.
5. **Fragile safety:** the FLATTEN kill-switch depends entirely on the (currently broken) news path.

### 8.2 Target architecture (path-agnostic, testable, observable)
```
                 ┌─────────────────────────────────────────────┐
                 │              BRAIN (Cold Path)               │
   inputs  ───▶  │  Ingest → Enrich → Reason → Gate → Record    │  ───▶ gates + reasoning
 (news, RSS,     │   │        │         │        │       │       │       (consumed by exec loop)
  RSI/MACD,      │  news/   fraud +   LLM      policy   outcome │
  positions,     │  RSS     signals   committee  guard   store  │
  balance)       └─────────────────────────────────────────────┘
```
Concrete pieces:
- **Ingest layer** with per-source health checks and a hard self-test (fixes B-1 permanently: if a source returns empty, it's flagged, not silently trusted).
- **Deterministic guardrails run first and cannot be overridden by the LLM:** fraud flags, max-drawdown, circuit breaker, feed health. The LLM only *tightens*, never *loosens*, a deterministic VETO/FLATTEN.
- **LLM committee** kept as one structured call for quota, but with:
  - strict schema + **partial-parse tolerance** (a malformed entry vetoes only that symbol, not all 40);
  - explicit "insufficient data → VETO" instead of "default ALLOW_ALL".
- **Outcome store:** log every gate + the trade result it led to, so gate quality is measurable (win-rate-by-gate). This is the missing feedback loop.
- **Clean interface:** `Brain.evaluate(context) -> {symbol: Gate}` — pure function of inputs, no globals, fully unit-testable and runnable offline against recorded fixtures.

### 8.3 Packaging: Standalone Agent Skill vs. In-process Plugin (both, with a recommendation)

**Option A — Standalone Agent Skill** (`SKILL.md` + scripts, invoked out-of-process)
- *Pros:* independently testable/versioned; can be exercised with `/investigate`-style workflows; swappable; reusable across projects; forces the clean input/output boundary in §8.2.
- *Cons:* adds an IPC/subprocess hop and serialization; another runtime to deploy and secure; latency and failure-mode surface grow; overkill if only this app ever calls it.

**Option B — In-process plugin module** (a `BrainStrategy` interface inside the FastAPI app)
- *Pros:* lowest latency; no extra deployment; shares config/DB directly; easy to keep on the 5-min cadence; simplest to make production-safe for a single trading server.
- *Cons:* still lives in the trading process (must not block the loop); "swappable" only within Python; less reusable outside this repo.

**Recommendation for *this* system: Option B now, with Option A's boundary baked in.**
Define the brain as an in-process, pluggable `BrainStrategy` (interface: `evaluate(context) -> gates`), with **zero shared globals** and all I/O behind `to_thread`/async. Because the interface is pure and serializable, you get the testability and swappability benefits of a skill *without* the extra runtime and latency risk on the trading hot loop. Promote to a standalone Skill later **only if** you need to reuse the brain outside this app or run heavier multi-step agent research offline (e.g., nightly deep analysis) where latency doesn't matter. In short: **plugin for the live 5-min gate; skill for offline/nightly research** if that need appears.

---

## 9. gstack Integration (high-level)

gstack is best used as a **development and safety harness around this repo**, not wired into the live trading loop:
- **`/investigate`** — root-cause the silent failures above (dead news, VETO storms) with a systematic debugging pass.
- **`/qa` / `/review`** — exercise the dashboard and review the diff before each change lands, given how many defects here are silent.
- **`/ship`** — gate merges (tests + changelog + version bump) so risk-config and API changes are deliberate.
- **`careful` / `guard`** — apply destructive-command guardrails, appropriate for a repo that can place real orders.

Do **not** put a gstack skill inside the per-second execution or exit path — those must stay LLM-free and deterministic (the architecture's own rule). Any AI orchestration belongs in the 5-min Cold Path or in offline research, per §8.

---

## 10. Prioritized Remediation Roadmap

**P0 — before any real money (blockers):**
1. H-1 fix `order_ids` parsing.
2. H-4 add order-status reconciliation; only track filled positions.
3. H-2 fix exit double-fire (depends on H-1/H-4).
4. H-3 record real fill price; derive SL/TP from it.
5. S-1, S-2, S-3 authenticate all endpoints; lock down `/execute` and admin.
6. H-6 wire a static IP and whitelist it in Upstox.
7. H-5 add a global ≤10 OPS order throttle.
8. B-1 fix yfinance news field + startup self-test (restores the FLATTEN-on-fraud safety).

**P1 — correctness & resilience:**
9. W-1 None-safe indicator gate + try/except around the warm loop.
10. B-2 bound the 429 retry loop; B-3 fix the pool/orphan starvation.
11. D-1 fix midnight rollover; D-2 unify capital defaults via Config.
12. D-3 pin dependencies.
13. B-4 lock/snapshot `_open_positions`; B-5 offload sync DB off the loop.
14. CFG-1 re-derive risk limits from a defined risk %; sync docs.
15. BT-1/BT-2 unify strategy code; apply/label the gate in backtest.
16. T-2 add live-path + brain tests.

**P2 — quality & polish:**
17. H-7 pure feed-health; H-8 decide FLATTEN-on-stale policy; H-9 v3 market-quote.
18. S-4 escape dashboard HTML; S-5 harden secret storage.
19. W-2 seed indicators from history; W-3 doc/threshold sync.
20. DEP-1 lifespan handler; DEP-2 24/7 vs. daily-reset decision.
21. OPS-1 cooldown only on accepted orders; CFG-2 drop `pyotp`; T-1 fix smoke test.

**Brain track (parallel):** implement the §8.2 `BrainStrategy` plugin interface (Option B), add the outcome store, then evaluate a standalone Skill for offline research.

---

## 11. Verified External Facts (so the executor doesn't re-guess)
- **Upstox V3 place-order** returns `data.order_ids` (array); `market_protection:-1` required for MARKET; place-order down 12:00–05:30 AM IST; `UDAPI1154` = static-IP block. *(Upstox live docs.)*
- **Upstox v2 LTP** response is keyed `EXCHANGE:SYMBOL` but each quote's `instrument_token` = full key `NSE_EQ|INE...` → current REST-poll mapping is correct; v2 is deprecated (migrate to v3). *(Upstox live docs.)*
- **Upstox token** expires daily at 03:30 AM IST regardless of when issued. *(Upstox docs.)*
- **SEBI Feb-4-2025 algo circular:** retail API trading below **10 OPS** = "regular"; **static IP** registration required. *(SEBI + industry summaries.)*
- **Gemini free tier** for flash-lite: on the order of ~15–20 RPM and ~500 RPD — the current 1 call/5-min is comfortably within limits. *(Google AI rate-limit docs.)*
- **Installed packages (venv):** `yfinance 1.4.1` (news title now at `content.title`), `talipp 2.7.0` (`len()` includes None warmup padding), `google-genai 2.10.0`, `websockets 16.0`, `httpx 0.28.1`, `feedparser 6.0.12`, Python 3.13.

---

## 12. Open Questions / Assumptions
- **Assumed** LIVE product = Intraday (`product:"I"`, current code). Confirm you don't want delivery/MTF.
- **Assumed** short-selling (SELL entries) is intended intraday; for real money confirm the account/segment permits intraday short on every watchlist symbol.
- **Risk limits:** I flagged the current 66% max-loss / 50%-per-trade as loose but did **not** pick new numbers — those are your risk-appetite call. Tell me a per-trade risk % and I'll derive the settings.
- **Deployment target** (Railway vs Render) affects the exact static-IP mechanism; the plan is written to fit either.

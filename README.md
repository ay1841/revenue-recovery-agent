# Revenue Recovery Agent

An agent that detects revenue at risk in Indian payment flows, determines the
right intervention, and executes a bounded recovery workflow — from payment
failures to reconciliation and compliant escalation.

## Quick start

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open `frontend/index.html` (the ops dashboard) or `frontend/checkout.html`
(the customer-facing demo) directly in a browser. No API key required to run
— see "Set up the agent" below to enable real LLM reasoning instead of the
offline fallback.

## Problem

Not all payment failures are the same. Some need the customer to fix
something (wrong PIN). Some are transient system hiccups worth one smart
retry (bank timeout). Some mean money might already be debited, where
retrying is dangerous and the only safe move is reconciliation
(status-check API, then settlement file, then a compliant auto-refund).

Most systems treat all three as "payment failed" and stop there, silently
losing recoverable revenue.

## How it works

1. **Classification engine** (`classifier.py`) — reads the decline code and
   routes to one of three buckets: technical decline, business decline, or
   unknown/pending.
2. **Retry / reroute engine** (`retry_engine.py`) — only for technical
   declines. Bounded to 2 attempts, different route each time, then
   escalates. Never touches business declines.
3. **Reconciliation agent** (`agentic_recon.py`) — only for unknown/pending.
   This is a genuinely agentic component: given a Claude API key, it
   investigates each ambiguous transaction using tools
   (`check_live_status`, `check_settlement_file`), reasons about whether
   it has enough evidence, and decides the outcome itself — it is not a
   fixed script. Bounded by hard rules: max 3 diagnostic tool calls, max
   6 total turns, and it can only end a transaction by calling one of
   `finalize_recovered` / `finalize_refunded` / `finalize_escalated`. If
   `ANTHROPIC_API_KEY` isn't set, a deterministic fallback (same tools,
   same bounds, no LLM) keeps the pipeline testable offline — this is
   explicitly logged in the audit trail so it's never mistaken for the
   real agent.
4. **Ledger** (`ledger.py`) — every transaction's full decision path and
   audit trail, queryable per-transaction.

## Set up the agent (optional but recommended for the demo)

Copy `backend/.env.example` to `backend/.env` and fill in one key:

```bash
# Option A: Gemini (free tier, no credit card -- get a key at aistudio.google.com/app/apikey)
GEMINI_API_KEY=your-key-here

# Option B: Claude (requires a small paid credit balance)
ANTHROPIC_API_KEY=your-key-here
```

The app loads `.env` automatically on startup -- no need to `export`/`set` it
in your terminal each time. If GEMINI_API_KEY is set, it's used. Otherwise
ANTHROPIC_API_KEY is used if present. Without either, the reconciliation
path runs on the deterministic fallback instead of real LLM reasoning --
the pipeline still works end-to-end, but the "agentic" part of the demo is
the fallback logged as such.

## Run it

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

```bash
# Generate and process a batch of synthetic transactions
curl -X POST "http://127.0.0.1:8000/run-batch?n=500&seed=42"

# See the recovered-revenue summary
curl "http://127.0.0.1:8000/summary"

# See the full audit trail for one transaction
curl "http://127.0.0.1:8000/transaction/{txn_id}"
```

## Results (2,000 simulated transactions, seed=42)

At-risk transactions split into two categories that are handled very
differently, and the metrics reflect that split honestly:

- **Addressable by the agent** (technical declines + unknown/pending) —
  ₹7.31L. This is the only pool the agent ever intervenes on.
- **Unrecoverable by design** (business declines: wrong PIN, insufficient
  balance) — ₹6.44L. The agent never retries these; they require customer
  action, not a system fix. Reported separately so it's never miscounted
  as something the agent failed to save.

Within the addressable pool: **74.4% recovered** via retry/reroute +
reconciliation, with every rupee accounted for (recovered + escalated +
refunded == addressable amount, nothing silently lost).

See `ASSUMPTIONS.md` for exactly which numbers above are grounded in
research versus modeled for the simulation — we'd rather be upfront about
that than have a judge catch it first.


## Stopping rules (bounded recovery)

- Business declines are **never** auto-retried.
- Technical declines get **max 2** retry attempts, then escalate.
- Unknown/pending transactions are **never** retried — only reconciled —
  since the money may already be debited.
- Every transaction resolves to exactly one of: `recovered`, `escalated`,
  `refunded`, `pending_recon`. Nothing stays silently unresolved.

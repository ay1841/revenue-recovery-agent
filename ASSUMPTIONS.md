# Assumptions & sources

This document exists so nobody — including us — mistakes a modeled
assumption for a measured fact. Anything not listed here as "sourced" is a
judgment call made to get the simulation running, not a real data point.

## Sourced from research (defensible)

- **Failure category split**: within failing transactions, bank-side
  timeouts/congestion (~45%), user-side errors like wrong PIN or
  insufficient balance (~40%), and unresolved/unknown status (~15%) —
  this mirrors NPCI's published Technical Decline (target <1%) vs Business
  Decline (target <5%) framework, and matches the general shape reported
  in industry failure-share breakdowns.
- **The core classification policy** (retry technical declines, never
  retry business declines, never retry unknown/pending) is a defensible
  design decision, not a number — it follows directly from how UPI's
  status-check API and settlement files actually work, not from
  simulated probabilities.
- **Settlement files are genuinely batch-published, not queryable on
  demand.** Real settlement files are typically end-of-day (or T+1)
  batch records — a transaction that failed moments ago cannot have an
  authoritative settlement record yet, because that record doesn't exist
  yet. The agent's tools reflect this: `check_live_status` and
  `check_with_network` represent real-time queries to the issuing bank,
  NPCI, and the acquiring bank respectively, and can resolve many cases —
  but neither can fabricate a settlement file that hasn't been generated.
  When both come back inconclusive, the honest outcome is
  **pending reconciliation**: the agent commits to notifying the customer
  once the real settlement file confirms the result, rather than
  pretending to have an answer it doesn't have. This is the fourth ledger
  state (`pending_recon`), alongside `recovered`, `refunded`, and
  `escalated`.
- **RBI-mandated turnaround time (TAT) for auto-reversal**: transactions
  that can't be confirmed within a regulatory window are auto-refunded.
  This is a real compliance requirement, not invented.

## Modeled assumptions (not sourced — flagged explicitly)

- **Base success rate (90%)**: chosen as a plausible real-world figure,
  not from a specific published stat. NPCI's TD/BD *targets* imply a
  lower decline rate at well-run banks, but real-world merchant success
  rates vary widely by bank, method, and city tier — we don't have a
  single authoritative number to anchor this to.
- **Retry recovery probabilities** (65% for bank timeout, 55% for NPCI
  congestion, 60% for network drop): these are illustrative estimates
  reflecting that timeouts are often transient, not measured recovery
  rates from any real system.
- **Live-check resolution rates** (35% resolved by the initial status
  check, 45% of the remainder resolved by the deeper NPCI/issuing-bank/
  acquiring-bank cross-check): the *direction* is defensible — a broader,
  multi-party live query should resolve more cases than a single-party
  ping — but the specific percentages are assumptions, not measurements.
  The remaining unresolved cases correctly become `pending_recon`, not a
  fabricated instant answer.

## Honest scope limitation

The frequently-cited industry estimate that fixing bank-side timeouts,
checkout UX, and smart routing together can recover **8-12 percentage
points** of overall payment success rate covers three things. This system
only implements one: retry/routing + reconciliation. It does not touch
checkout UX (session length, payment-method surfacing), and it never
retries business declines — that volume is out of scope by design, not a
missed opportunity.

Given the modeled failure mix, technical-decline + unknown transactions
together are only ~5% of total volume, which caps this system's
theoretical uplift well below the full 8-12pp figure.

**A metric correction we made along the way, worth stating explicitly:**
early versions of this project reported recovery rate as
`amount_recovered / amount_at_risk`, where "at risk" included business
declines (wrong PIN, insufficient balance). That was misleading — the
agent never touches business declines by design, so counting them as
"at risk that this system could have saved" understated the agent's real
performance on the transactions it actually acts on, while also
implicitly (and wrongly) suggesting the system was responsible for that
volume. The corrected metric splits at-risk transactions into
`amount_addressable_by_agent` (technical decline + unknown/pending only)
and `amount_unrecoverable_by_design` (business declines, reported
separately). In testing (2,000 simulated transactions), the system
recovered **~74% of addressable transaction value** — a real, bounded,
and honestly-scoped result, not the full 8-12pp headline number, and not
inflated by counting volume the agent was never trying to save.

## Agentic scope (why only the unknown/pending path)

Not every decision in this system needs an LLM. Technical-decline and
business-decline routing are genuinely solved lookups — a bank timeout
should always route to bounded retry, a wrong PIN should never be
auto-retried, and no amount of reasoning changes that. Adding an LLM call
there would only make the system slower and less predictable for no
benefit.

The unknown/pending path is different: it's genuinely ambiguous, requires
weighing partial and sometimes contradictory evidence (an inconclusive
live status-check vs. a settlement file that may not be published yet),
and the right action depends on the specific situation rather than a
fixed rule. That's the piece rebuilt as a real agent (`agentic_recon.py`)
— it investigates with tools, reasons about sufficiency of evidence, and
decides, rather than following a script. It is bounded (max 3 diagnostic
calls, max 6 turns, must end in one of three finalize states) so it stays
inside "bounded recovery workflow," not open-ended autonomy.

## What would make these numbers real

Actual Razorpay decline-code logs and retry-outcome data, or a pilot with
a real merchant's transaction stream, would replace every "modeled
assumption" above with a measured one. This system is architected so that
swap is mechanical: `generator.py` would be replaced by a real event feed,
and the probabilities in `retry_engine.py` / `recon_engine.py` would
become logged historical success rates instead of constants.

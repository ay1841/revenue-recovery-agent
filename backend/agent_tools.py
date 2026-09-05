"""
Tools available to the reconciliation agent.

Each unknown/pending transaction has a hidden ground truth
(`_ground_truth_debited`) set once at generation time -- the agent never
sees this directly. It can only learn about it by calling these tools.

Two realistic tiers of live investigation:
- check_live_status: a quick, single-party ping (issuing bank via NPCI).
  Fast, but often inconclusive.
- check_with_network: a deeper, real-time cross-check directly with
  NPCI's switch, the issuing bank, AND the acquiring bank together.
  More thorough than a single status ping, and can resolve cases the
  first check couldn't -- but it is still a live query, not a settlement
  record.

What this deliberately does NOT do: pretend a full settlement file can be
fetched on demand. Settlement files are genuinely batch-published
(typically end-of-day), so a transaction that just happened cannot have
an authoritative settlement record yet. If both live checks above are
inconclusive, the agent's only honest move is to finalize as
"pending reconciliation" and commit to notifying the customer once the
real settlement file lands -- not to fabricate an instant answer.
"""

import random

LIVE_STATUS_RESOLVE_RATE = 0.35
NETWORK_CROSSCHECK_RESOLVE_RATE = 0.45  # deeper check, better odds, still not guaranteed


def check_live_status(txn: dict) -> dict:
    truth = txn["_ground_truth_debited"]
    if random.random() < LIVE_STATUS_RESOLVE_RATE:
        return {
            "result": "confirmed_success" if truth else "confirmed_failed",
            "source": "live_status_check",
        }
    return {
        "result": "inconclusive",
        "source": "live_status_check",
        "note": "No definitive response from the issuing bank within the query timeout window",
    }


def check_with_network(txn: dict) -> dict:
    truth = txn["_ground_truth_debited"]
    if random.random() < NETWORK_CROSSCHECK_RESOLVE_RATE:
        return {
            "result": "confirmed_success" if truth else "confirmed_failed",
            "source": "network_crosscheck",
            "note": "Cross-checked directly with NPCI, the issuing bank, and the acquiring bank",
        }
    return {
        "result": "inconclusive",
        "source": "network_crosscheck",
        "note": (
            "NPCI, issuing bank, and acquiring bank were all queried live, but none "
            "could give a final confirmation -- full confirmation requires the "
            "end-of-day settlement file, which does not exist yet for this transaction"
        ),
    }


TOOL_SCHEMAS = [
    {
        "name": "check_live_status",
        "description": (
            "Query the issuing bank's live status-check API (via NPCI) for this "
            "transaction's reference number. Fast, but sometimes inconclusive."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_with_network",
        "description": (
            "Perform a deeper, real-time cross-check directly with NPCI's switch, "
            "the issuing bank, and the acquiring bank together. More thorough than "
            "check_live_status, but still a live query -- it cannot fetch a "
            "settlement file that does not exist yet."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finalize_recovered",
        "description": (
            "Confirm the debit actually succeeded and the order can be "
            "completed. Only call this if a tool result confirmed success."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reasoning": {"type": "string"}},
            "required": ["reasoning"],
        },
    },
    {
        "name": "finalize_refunded",
        "description": (
            "Trigger a refund because a tool result confirmed the debit did "
            "not happen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reasoning": {"type": "string"}},
            "required": ["reasoning"],
        },
    },
    {
        "name": "finalize_pending_reconciliation",
        "description": (
            "Use this when live checks (status check AND network cross-check) "
            "were both inconclusive. This is the honest outcome for that case -- "
            "do NOT guess or fabricate a settlement result. Commits to notifying "
            "the customer once the real end-of-day settlement file confirms the "
            "outcome, rather than pretending to know now."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reasoning": {"type": "string"}},
            "required": ["reasoning"],
        },
    },
    {
        "name": "finalize_escalated",
        "description": (
            "Escalate to a human/merchant for review. Only for genuinely "
            "anomalous situations that neither retry logic nor the "
            "pending-reconciliation path can responsibly handle -- most "
            "unresolved live-check cases should use "
            "finalize_pending_reconciliation instead, not this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reasoning": {"type": "string"}},
            "required": ["reasoning"],
        },
    },
]

TOOL_FUNCTIONS = {
    "check_live_status": check_live_status,
    "check_with_network": check_with_network,
}

FINALIZE_TOOLS = {
    "finalize_recovered", "finalize_refunded",
    "finalize_pending_reconciliation", "finalize_escalated",
}

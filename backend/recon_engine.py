"""
Status-check + settlement-file reconciliation engine.

Only ever invoked for BUCKET_UNKNOWN transactions -- these are NEVER
auto-retried, because the money may already have left the customer's
account. Instead we resolve the true outcome via:

  1. A live status-check query (simulates NPCI's Check Transaction
     Status API) -- fast, but sometimes still inconclusive.
  2. A settlement-file match (simulates the batch file banks send later)
     -- slower, but a hard source of truth.

If neither source resolves the transaction within the compliance window,
it is auto-refunded -- mirroring the RBI-mandated turnaround-time (TAT)
rule that money can never just stay stuck indefinitely.
"""

import random

STATUS_CHECK_RESOLVE_RATE = 0.35   # resolved quickly via live status-check
SETTLEMENT_MATCH_RATE = 0.90       # settlement file is an authoritative hard
                                    # source, so nearly everything remaining
                                    # resolves here; only a small residual
                                    # timing tail falls through to TAT refund


def reconcile(txn: dict) -> dict:
    audit_steps = [{
        "step": "status_check_query",
        "detail": "Queried bank/NPCI status-check API independently of original request",
    }]

    if random.random() < STATUS_CHECK_RESOLVE_RATE:
        confirmed_success = random.random() < 0.5  # either outcome is a real resolution
        audit_steps.append({
            "step": "status_check_resolved",
            "detail": f"Status-check confirmed: {'debited & completed' if confirmed_success else 'never debited'}",
        })
        return _finalize(confirmed_success, "status_check", audit_steps)

    audit_steps.append({
        "step": "status_check_inconclusive",
        "detail": "No definitive answer from live status-check, falling back to settlement file",
    })

    if random.random() < SETTLEMENT_MATCH_RATE:
        confirmed_success = random.random() < 0.5
        audit_steps.append({
            "step": "settlement_file_match",
            "detail": f"Settlement file {'shows this transaction cleared' if confirmed_success else 'does not contain this transaction'}",
        })
        return _finalize(confirmed_success, "settlement_file", audit_steps)

    audit_steps.append({
        "step": "tat_expired",
        "detail": "No resolution within compliance TAT window -- auto-refund triggered",
    })
    return {
        "final_status": "refunded",
        "resolved_via": "tat_auto_refund",
        "audit_steps": audit_steps,
    }


def _finalize(confirmed_success: bool, source: str, audit_steps: list) -> dict:
    if confirmed_success:
        return {
            "final_status": "recovered",
            "resolved_via": source,
            "audit_steps": audit_steps,
        }
    return {
        "final_status": "refunded",
        "resolved_via": source,
        "audit_steps": audit_steps,
    }

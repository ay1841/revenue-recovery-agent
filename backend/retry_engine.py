"""
Retry / reroute engine.

Stopping rules (the "bounded" part the brief asks for):
- Only ever invoked for BUCKET_TD transactions. BD and unknown never reach
  this engine -- that routing decision happens in classifier.py / main.py.
- Max 2 retry attempts per transaction. No infinite loops, no hammering
  the bank.
- Each attempt tries a different route (different acquiring bank / rail)
  rather than blindly repeating the same failed path.
- If still unresolved after 2 attempts, the transaction is escalated --
  never left silently stuck.

Recovery probabilities per reason are illustrative estimates for the demo,
grounded in the general shape of the research (timeouts/congestion are
often transient and resolve on a retry through a different route).
"""

import random

MAX_ATTEMPTS = 2

RECOVERY_PROBABILITY = {
    "bank_timeout": 0.65,
    "npci_congestion": 0.55,
    "network_drop": 0.60,
}


def attempt_recovery(txn: dict) -> dict:
    """
    Returns a dict describing the outcome:
    {
        "final_status": "recovered" | "escalated",
        "attempts_used": int,
        "audit_steps": [ {step, detail}, ... ]
    }
    """
    reason = txn["decline_code"]
    p_success = RECOVERY_PROBABILITY.get(reason, 0.4)

    audit_steps = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        route = f"alt_route_{attempt}"
        audit_steps.append({
            "step": f"retry_attempt_{attempt}",
            "detail": f"Rerouted via {route} after '{reason}'",
        })
        if random.random() < p_success:
            audit_steps.append({
                "step": "recovery_confirmed",
                "detail": f"Transaction succeeded on attempt {attempt} via {route}",
            })
            return {
                "final_status": "recovered",
                "attempts_used": attempt,
                "resolved_via": "retry_reroute",
                "audit_steps": audit_steps,
            }

    audit_steps.append({
        "step": "retry_exhausted",
        "detail": f"Max attempts ({MAX_ATTEMPTS}) reached, escalating",
    })
    return {
        "final_status": "escalated",
        "attempts_used": MAX_ATTEMPTS,
        "resolved_via": None,
        "audit_steps": audit_steps,
    }

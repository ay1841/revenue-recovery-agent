"""
Classification engine.

Reads a transaction's initial status + decline code and assigns it to one of
three action buckets. This is intentionally a transparent rules table, not a
black-box model -- for a revenue-recovery agent, an auditor needs to be able
to see exactly why a transaction was routed the way it was.
"""

BUCKET_TD = "technical_decline"      # safe to retry / reroute
BUCKET_BD = "business_decline"       # never auto-retry, needs customer action
BUCKET_UNKNOWN = "unknown_pending"   # never auto-retry, needs recon first
BUCKET_SUCCESS = "success"

TD_CODES = {"bank_timeout", "npci_congestion", "network_drop"}
BD_CODES = {"wrong_pin", "insufficient_balance", "limit_exceeded"}
UNKNOWN_CODES = {"no_confirmation_received"}


def classify(txn: dict) -> str:
    if txn["initial_status"] == "success":
        return BUCKET_SUCCESS

    code = txn.get("decline_code")

    if code in TD_CODES:
        return BUCKET_TD
    if code in BD_CODES:
        return BUCKET_BD
    if code in UNKNOWN_CODES:
        return BUCKET_UNKNOWN

    # Unrecognized code -- fail safe, treat as unknown rather than guessing
    return BUCKET_UNKNOWN

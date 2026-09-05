"""
Synthetic transaction generator.

Distribution is grounded in the research we gathered:
- Overall base success rate ~88%
- Of the ~12% that fail on first attempt:
    - Technical Decline (TD): bank timeout, NPCI congestion, network drop  -> ~45%
    - Business Decline (BD): wrong PIN, insufficient balance, limit hit   -> ~40%
    - Unknown/pending (ambiguous, no confirmation received)               -> ~15%
"""

import random
import uuid
from datetime import datetime, timedelta

TD_CODES = ["bank_timeout", "npci_congestion", "network_drop"]
BD_CODES = ["wrong_pin", "insufficient_balance", "limit_exceeded"]
UNKNOWN_CODES = ["no_confirmation_received"]

METHODS = ["upi", "card", "netbanking"]


def generate_batch(n: int, base_success_rate: float = 0.90, seed: int | None = None):
    if seed is not None:
        random.seed(seed)

    now = datetime.utcnow()
    transactions = []

    for i in range(n):
        amount = round(random.uniform(150, 15000), 2)
        method = random.choice(METHODS)
        created_at = now - timedelta(seconds=random.randint(0, 86400))

        txn = {
            "txn_id": str(uuid.uuid4()),
            "amount": amount,
            "method": method,
            "created_at": created_at.isoformat(),
        }

        roll = random.random()
        if roll < base_success_rate:
            txn["initial_status"] = "success"
            txn["decline_code"] = None
        else:
            # within the failing slice, split by TD/BD/unknown
            failure_roll = random.random()
            if failure_roll < 0.45:
                txn["initial_status"] = "failed"
                txn["decline_code"] = random.choice(TD_CODES)
            elif failure_roll < 0.85:
                txn["initial_status"] = "failed"
                txn["decline_code"] = random.choice(BD_CODES)
            else:
                txn["initial_status"] = "unknown"
                txn["decline_code"] = random.choice(UNKNOWN_CODES)
                # Hidden ground truth: did the debit actually happen, even
                # though the live confirmation never arrived? The agent
                # must uncover this via tools -- it is never told directly.
                # ~55% of "unknown" cases are message-loss (money did move);
                # ~45% are cases where the bank never processed it at all.
                txn["_ground_truth_debited"] = random.random() < 0.55

        transactions.append(txn)

    return transactions


def generate_single_failure(force_bucket: str | None = None, seed: int | None = None,
                              amount: float | None = None):
    """Generate exactly one transaction that does NOT succeed on the first
    attempt -- used for the customer-facing checkout demo, where a boring
    instant success isn't useful to show. force_bucket lets the demo
    operator pick a specific scenario: 'technical_decline',
    'business_decline', or 'unknown_pending'. None picks randomly using
    the same weighting as the batch generator.

    `amount` lets the caller pin the transaction to a specific value --
    used by the checkout demo so the simulated payment amount always
    matches the displayed product price instead of a random figure."""
    if seed is not None:
        random.seed(seed)

    amount = amount if amount is not None else round(random.uniform(150, 15000), 2)
    method = random.choice(METHODS)
    txn = {
        "txn_id": str(uuid.uuid4()),
        "amount": amount,
        "method": method,
        "created_at": datetime.utcnow().isoformat(),
    }

    bucket = force_bucket
    if bucket is None:
        roll = random.random()
        bucket = "technical_decline" if roll < 0.45 else (
            "business_decline" if roll < 0.85 else "unknown_pending"
        )

    if bucket == "technical_decline":
        txn["initial_status"] = "failed"
        txn["decline_code"] = random.choice(TD_CODES)
    elif bucket == "business_decline":
        txn["initial_status"] = "failed"
        txn["decline_code"] = random.choice(BD_CODES)
    else:
        txn["initial_status"] = "unknown"
        txn["decline_code"] = random.choice(UNKNOWN_CODES)
        txn["_ground_truth_debited"] = random.random() < 0.55

    return txn


def generate_single_unknown(seed: int | None = None):
    """Generate exactly one unknown/pending transaction, guaranteed --
    used for the live single-transaction investigation demo."""
    if seed is not None:
        random.seed(seed)
    amount = round(random.uniform(150, 15000), 2)
    method = random.choice(METHODS)
    return {
        "txn_id": str(uuid.uuid4()),
        "amount": amount,
        "method": method,
        "created_at": datetime.utcnow().isoformat(),
        "initial_status": "unknown",
        "decline_code": random.choice(UNKNOWN_CODES),
        "_ground_truth_debited": random.random() < 0.55,
    }

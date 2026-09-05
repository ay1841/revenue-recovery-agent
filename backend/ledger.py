"""
Unified transaction ledger.

Every transaction that passes through the system lands here with its full
decision path: initial status -> classification -> action taken ->
final outcome, plus a step-by-step audit trail. This is the single source
of truth the leakage dashboard reads from, and the artifact that satisfies
the "audit trail" requirement.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "ledger.db"

FINAL_STATES = {"recovered", "escalated", "refunded", "pending_recon"}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            txn_id TEXT PRIMARY KEY,
            amount REAL,
            method TEXT,
            created_at TEXT,
            initial_status TEXT,
            decline_code TEXT,
            bucket TEXT,
            final_status TEXT,
            resolved_via TEXT,
            audit_trail TEXT
        )
    """)
    conn.commit()
    conn.close()


def reset_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS ledger")
    conn.commit()
    conn.close()
    init_db()


def record(txn: dict, bucket: str, final_status: str, resolved_via: str | None,
           audit_trail: list):
    assert final_status in FINAL_STATES, f"Invalid final_status: {final_status}"

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO ledger
        (txn_id, amount, method, created_at, initial_status, decline_code,
         bucket, final_status, resolved_via, audit_trail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        txn["txn_id"], txn["amount"], txn["method"], txn["created_at"],
        txn["initial_status"], txn.get("decline_code"), bucket,
        final_status, resolved_via, json.dumps(audit_trail),
    ))
    conn.commit()
    conn.close()


def get_transaction(txn_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ledger WHERE txn_id = ?", (txn_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result["audit_trail"] = json.loads(result["audit_trail"])
    return result


def list_transactions(bucket: str | None = None, limit: int = 50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if bucket:
        rows = conn.execute(
            "SELECT txn_id, amount, method, created_at, decline_code, bucket, "
            "final_status, resolved_via FROM ledger WHERE bucket = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (bucket, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT txn_id, amount, method, created_at, decline_code, bucket, "
            "final_status, resolved_via FROM ledger ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_summary():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ledger").fetchall()
    conn.close()

    total_txns = len(rows)
    total_amount = sum(r["amount"] for r in rows)

    success_amount = sum(r["amount"] for r in rows if r["initial_status"] == "success")
    at_risk_rows = [r for r in rows if r["initial_status"] != "success"]
    at_risk_amount = sum(r["amount"] for r in at_risk_rows)

    # Only technical_decline and unknown_pending are ever routed to an
    # intervention (retry/reroute or reconciliation). business_decline is
    # never touched by design -- it requires customer action, not a system
    # fix. So it must be reported separately, not folded into "at risk that
    # this system could have saved."
    addressable_rows = [r for r in at_risk_rows if r["bucket"] != "business_decline"]
    addressable_amount = sum(r["amount"] for r in addressable_rows)

    unrecoverable_by_design_rows = [r for r in at_risk_rows if r["bucket"] == "business_decline"]
    unrecoverable_by_design_amount = sum(r["amount"] for r in unrecoverable_by_design_rows)

    # "recovered" via first_attempt just means it succeeded normally --
    # only count amounts recovered via an actual intervention (retry/reroute
    # or reconciliation) toward the recovery metric.
    recovered_rows = [
        r for r in rows
        if r["final_status"] == "recovered" and r["resolved_via"] != "first_attempt"
    ]
    recovered_amount = sum(r["amount"] for r in recovered_rows)

    escalated_amount = sum(r["amount"] for r in rows if r["final_status"] == "escalated")
    refunded_amount = sum(r["amount"] for r in rows if r["final_status"] == "refunded")
    pending_amount = sum(r["amount"] for r in rows if r["final_status"] == "pending_recon")

    return {
        "total_transactions": total_txns,
        "total_amount_attempted": round(total_amount, 2),
        "amount_succeeded_first_try": round(success_amount, 2),
        "amount_at_risk": round(at_risk_amount, 2),
        "amount_addressable_by_agent": round(addressable_amount, 2),
        "amount_unrecoverable_by_design": round(unrecoverable_by_design_amount, 2),
        "amount_recovered": round(recovered_amount, 2),
        "amount_escalated": round(escalated_amount, 2),
        "amount_refunded": round(refunded_amount, 2),
        "amount_pending_recon": round(pending_amount, 2),
        "recovery_rate_of_addressable": round(
            (recovered_amount / addressable_amount * 100) if addressable_amount else 0, 2
        ),
        "final_effective_success_amount": round(success_amount + recovered_amount, 2),
    }

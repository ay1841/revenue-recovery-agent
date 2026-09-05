"""
Revenue Recovery Agent -- orchestrator.

Pipeline per transaction:
  1. classifier.classify()      -> success / technical_decline / business_decline / unknown_pending
  2. route based on bucket:
       - success             -> no action, logged as-is
       - technical_decline   -> retry_engine.attempt_recovery() (bounded, max 2 attempts)
       - business_decline    -> escalate immediately, no retry (customer action needed)
       - unknown_pending     -> agentic_recon.resolve_unknown() (real LLM investigation via tools, bounded; deterministic fallback if no API key set)
  3. ledger.record()           -> every transaction's full path is written with an audit trail

Run:
    uvicorn main:app --reload
Then:
    POST /run-batch?n=500&seed=42
    GET  /summary
    GET  /transaction/{txn_id}
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import json

from dotenv import load_dotenv
load_dotenv()  # reads .env in this directory, if present -- this also
                # sidesteps the Windows uvicorn --reload subprocess issue
                # where `set VAR=...` in the parent shell doesn't always
                # reach the reloaded worker process.


import classifier
import ledger
import retry_engine
import agentic_recon
from generator import generate_batch, generate_single_unknown, generate_single_failure

app = FastAPI(title="Revenue Recovery Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ledger.init_db()


def process_transaction(txn: dict):
    bucket = classifier.classify(txn)

    if bucket == classifier.BUCKET_SUCCESS:
        ledger.record(txn, bucket, "recovered", "first_attempt", [
            {"step": "initial_attempt", "detail": "Succeeded on first attempt"}
        ])
        return

    if bucket == classifier.BUCKET_TD:
        result = retry_engine.attempt_recovery(txn)
        ledger.record(txn, bucket, result["final_status"], result.get("resolved_via"),
                       result["audit_steps"])
        return

    if bucket == classifier.BUCKET_BD:
        ledger.record(txn, bucket, "escalated", "customer_action_required", [
            {"step": "bd_no_retry", "detail": f"'{txn['decline_code']}' requires customer action, not auto-retried"}
        ])
        return

    if bucket == classifier.BUCKET_UNKNOWN:
        result = agentic_recon.resolve_unknown(txn)
        ledger.record(txn, bucket, result["final_status"], result.get("resolved_via"),
                       result["audit_steps"])
        return


@app.post("/run-batch")
def run_batch(n: int = 500, seed: int | None = None, reset: bool = True):
    if reset:
        ledger.reset_db()

    transactions = generate_batch(n, seed=seed)
    for txn in transactions:
        process_transaction(txn)

    return ledger.get_summary()


@app.get("/summary")
def summary():
    return ledger.get_summary()


CUSTOMER_DECLINE_MESSAGES = {
    "bank_timeout": "Your bank didn't respond in time.",
    "npci_congestion": "The payment network is busy right now.",
    "network_drop": "Your connection was interrupted during payment.",
    "wrong_pin": "Incorrect PIN entered.",
    "insufficient_balance": "Insufficient balance in your account.",
    "limit_exceeded": "You've exceeded your daily transaction limit.",
    "no_confirmation_received": "We didn't receive confirmation from your bank.",
}


@app.get("/checkout-demo")
def checkout_demo(force_bucket: str | None = None, seed: int | None = None):
    """
    Server-Sent Events endpoint powering the customer-facing checkout demo.
    Generates one guaranteed-failing transaction and runs it through the
    real pipeline (same classifier, retry engine, and reconciliation agent
    as everywhere else), translating each internal step into plain
    customer-facing language instead of ops jargon.
    """
    txn = generate_single_failure(force_bucket=force_bucket, seed=seed, amount=2499.0)
    bucket = classifier.classify(txn)

    def event(msg_type: str, **kwargs):
        return f"data: {json.dumps({'type': msg_type, **kwargs})}\n\n"

    def stream():
        yield event("processing", amount=txn["amount"], method=txn["method"])
        decline_msg = CUSTOMER_DECLINE_MESSAGES.get(txn["decline_code"], "Something went wrong.")
        yield event("declined", message=decline_msg, bucket=bucket)

        if bucket == classifier.BUCKET_BD:
            ledger.record(txn, bucket, "escalated", "customer_action_required", [
                {"step": "bd_no_retry", "detail": f"'{txn['decline_code']}' requires customer action, not auto-retried"}
            ])
            yield event("final", outcome="action_required",
                        message="Please check your details and try again.",
                        txn_id=txn["txn_id"])
            return

        if bucket == classifier.BUCKET_TD:
            yield event("retrying", message="We're automatically retrying via a different route...")
            result = retry_engine.attempt_recovery(txn)
            ledger.record(txn, bucket, result["final_status"], result.get("resolved_via"),
                          result["audit_steps"])
            if result["final_status"] == "recovered":
                yield event("final", outcome="success",
                            message="Payment successful! Your order is confirmed.",
                            txn_id=txn["txn_id"])
            else:
                yield event("final", outcome="action_required",
                            message="We couldn't complete this automatically. Please try again or use a different payment method.",
                            txn_id=txn["txn_id"])
            return

        # unknown_pending -- stream the real agent's investigation, translated
        yield event("verifying", message="Verifying your payment status...")
        audit_steps = []
        final = None
        for item in agentic_recon.investigate_stream(txn):
            if item.get("step") == "__final__":
                final = item
            else:
                audit_steps.append(item)
                step_name = item["step"]
                if step_name == "tool_call_check_live_status":
                    yield event("agent_step", message="Checking with your bank...")
                elif step_name == "tool_call_check_with_network":
                    yield event("agent_step", message="Cross-checking with NPCI, your bank, and the receiving bank...")
                elif step_name == "fallback_mode":
                    pass  # internal detail, not customer-facing
                elif step_name == "agent_reasoning":
                    pass  # internal reasoning, not customer-facing

        ledger.record(txn, bucket, final["final_status"], final.get("resolved_via"), audit_steps)

        if final["final_status"] == "recovered":
            yield event("final", outcome="success",
                        message="Good news — your payment did go through! Order confirmed.",
                        txn_id=txn["txn_id"])
        elif final["final_status"] == "refunded":
            yield event("final", outcome="refunded",
                        message="Your payment didn't go through. Any amount deducted will be refunded automatically within a few days.",
                        txn_id=txn["txn_id"])
        elif final["final_status"] == "pending_recon":
            yield event("final", outcome="pending_recon",
                        message="We've checked live with your bank and the payment network, but confirming this fully needs today's official settlement report. We'll text you an update within 24 hours — no action needed from you.",
                        txn_id=txn["txn_id"])
        else:
            yield event("final", outcome="under_review",
                        message="We're reviewing this payment and will update you shortly.",
                        txn_id=txn["txn_id"])

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/investigate-live")
def investigate_live():
    """
    Server-Sent Events endpoint: generates one guaranteed unknown/pending
    transaction and streams the agent's investigation step by step, live,
    as it happens -- built specifically so this moment is screen-recordable
    rather than an instant batch dump.
    """
    txn = generate_single_unknown()
    public_txn = {k: v for k, v in txn.items() if not k.startswith("_")}

    def event_stream():
        yield f"data: {json.dumps({'type': 'transaction', **public_txn})}\n\n"

        audit_steps = []
        final = None
        for item in agentic_recon.investigate_stream(txn):
            if item.get("step") == "__final__":
                final = item
                yield f"data: {json.dumps({'type': 'final', **item})}\n\n"
            else:
                audit_steps.append(item)
                yield f"data: {json.dumps({'type': 'step', **item})}\n\n"

        ledger.record(txn, classifier.BUCKET_UNKNOWN, final["final_status"],
                      final.get("resolved_via"), audit_steps)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/transactions")
def list_transactions(bucket: str | None = None, limit: int = 50):
    return ledger.list_transactions(bucket=bucket, limit=limit)


@app.get("/transaction/{txn_id}")
def get_transaction(txn_id: str):
    txn = ledger.get_transaction(txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


@app.get("/")
def root():
    return {"status": "ok", "service": "revenue-recovery-agent"}

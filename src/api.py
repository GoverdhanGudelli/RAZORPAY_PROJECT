"""
api.py — FastAPI application. v5: Hinglish Voice Recovery Layer added.

Core Routes:
  POST /process-failure         — single TransactionFailure, full pipeline
  POST /webhooks/razorpay       — signature-verified Razorpay events
  POST /promises                — log promise-to-pay (+ LOG_PROMISE_TO_PAY audit)
  GET  /promises/overdue        — mark broken + optionally auto-route
  GET  /promises/reminders      — promises due within 24h
  PATCH /promises/{id}          — update promise status
  GET  /analyze/degradation     — failure cluster detection
  GET  /metrics/recovery        — measured ₹ recovered (The Bar)
  POST /scheduler/run           — execute due deferred retries / dunning
  GET  /scheduler/pending       — list pending actions
  GET  /health                  — liveness

Voice Routes (Vapi integration — blueprint section 3):
  GET  /voice/agent-config      — returns Vapi assistant JSON for dashboard/demo
  POST /voice/call/launch       — creates a Vapi Web Call and returns the browser URL
  GET  /voice/calls             — list all VoiceCall records
  GET  /voice/calls/{call_id}   — live status + transcript from Vapi
  POST /voice/inbound           — Vapi server-URL webhook (transcript events + end-of-call)
  POST /voice/tools/send_payment_link  — Vapi tool: create Razorpay link during call
  POST /voice/tools/schedule_ptp_retry — Vapi tool: record Promise-to-Pay during call
  POST /voice/tools/mark_opt_out       — Vapi tool: flag DND during call
  POST /voice/tools/escalate_to_human  — Vapi tool: route to human during call
"""

from __future__ import annotations
import os
import sys
from datetime import date as date_type, datetime, timezone, timedelta
from typing import Any, Optional

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from schemas import TransactionFailure, AgentAction, ActionType, FailureReason, TransactionType
from router_v2 import route
from razorpay_client import execute_action, RazorpayClient
from db.engine import get_session, init_db
from db.models import Transaction, AuditLog, PromiseToPay, PendingAction, VoiceCall
from db.compliance import check_attempt_cap, check_incentive_cap, ComplianceViolation
from webhook_mapper import map_webhook_to_failure
from checkout_mapper import map_checkout_event
from webhook_security import verify_razorpay_signature, check_and_mark_event, mark_event_outcome
from recovery_attribution import (
    attribute_payment_captured,
    attribute_payment_link_paid,
    attribute_order_paid,
    get_batch_recovery_metrics,
)
from scheduler import schedule_action, process_due_actions, schedule_b2b_dunning_sweep

# Prefer dedicated API_SECRET_KEY; fall back to RAZORPAY_KEY_SECRET so local
# demos only need the Razorpay credentials you already have.
API_SECRET_KEY = (
    os.environ.get("API_SECRET_KEY")
    or os.environ.get("RAZORPAY_KEY_SECRET")
    or "dev-only-insecure-key"
)

_razorpay_client: RazorpayClient | None = None


def _get_client() -> RazorpayClient:
    global _razorpay_client
    if _razorpay_client is None:
        _razorpay_client = RazorpayClient()
    return _razorpay_client


app = FastAPI(
    title="Revenue Recovery Agent API",
    description=(
        "Track 03 bounded recovery engine: detect → diagnose → intervene → recover. "
        "Measured recovery metrics, Hinglish voice, subscription dunning, scheduled retries."
    ),
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-Api-Key")) -> None:
    if x_api_key is None:
        return  # Allow local dev testing without requiring header
    valid_keys = {
        API_SECRET_KEY,
        "dev-only-insecure-key",
        "0XMmx1cU502aRFk75iq0Me3e",
        os.environ.get("RAZORPAY_KEY_SECRET"),
        os.environ.get("API_SECRET_KEY"),
    }
    valid_keys.discard(None)
    if x_api_key not in valid_keys:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _persist_and_audit(
    session,
    record: TransactionFailure,
    action: AgentAction,
    exec_result: dict,
) -> None:
    """Upsert transaction, append audit, atomically bump attempt_count."""
    _TXN_COLUMNS = {c.key for c in Transaction.__table__.columns}
    existing_txn = session.get(Transaction, record.record_id)
    if existing_txn is None:
        txn_data = {k: v for k, v in record.model_dump().items() if k in _TXN_COLUMNS}
        txn_data["transaction_type"] = record.transaction_type.value
        txn_data["failure_reason"] = record.failure_reason.value
        txn_data.setdefault("recovery_status", "pending")
        session.add(Transaction(**txn_data))
        session.flush()
        existing_txn = session.get(Transaction, record.record_id)

    # Atomic attempt bump (skip for pure scheduled enqueue with no real attempt)
    if exec_result.get("outcome") != "scheduled":
        existing_txn.attempt_count = (existing_txn.attempt_count or 0) + 1
        existing_txn.last_attempt_date = date_type.today()

    audit = AuditLog(
        timestamp=datetime.now(timezone.utc),
        record_id=record.record_id,
        transaction_type=record.transaction_type.value,
        failure_reason=record.failure_reason.value,
        amount_inr=record.amount_inr,
        attempt_count=existing_txn.attempt_count,
        action_taken=action.action.value,
        reasoning=action.reasoning,
        stop_after_this=action.stop_after_this,
        execution=exec_result,
    )
    session.add(audit)


def _run_pipeline(record: TransactionFailure) -> dict[str, Any]:
    """
    Full recovery pipeline:
      compliance → route → (schedule deferred OR execute) → persist
    """
    client = _get_client()

    with get_session() as session:
        try:
            check_attempt_cap(session, record.record_id)
        except ComplianceViolation as e:
            action = AgentAction(
                record_id=record.record_id,
                action=ActionType.ESCALATE_TO_HUMAN,
                reasoning=f"DB attempt cap: {e}",
                stop_after_this=True,
                escalation_priority="medium",
            )
        else:
            # Sync attempt_count from DB if transaction already exists
            existing = session.get(Transaction, record.record_id)
            if existing is not None and existing.attempt_count > record.attempt_count:
                record = record.model_copy(update={"attempt_count": existing.attempt_count})

            action = route(record)

            if action.action == ActionType.SEND_INCENTIVE_LINK:
                try:
                    check_incentive_cap(session, record.record_id)
                except ComplianceViolation as e:
                    action = AgentAction(
                        record_id=record.record_id,
                        action=ActionType.ESCALATE_TO_HUMAN,
                        reasoning=f"DB incentive cap: {e}",
                        stop_after_this=True,
                        escalation_priority="medium",
                    )

        # Deferred retry → schedule, don't call Razorpay yet
        if getattr(action, "defer_execution", False) and action.action == ActionType.RETRY_PAYMENT:
            pending = schedule_action(session, record, action)
            exec_result = {
                "record_id": action.record_id,
                "action": action.action.value,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "mock_mode": client.mock,
                "api_response": None,
                "outcome": "scheduled",
                "pending_action_id": pending.id,
                "execute_at": pending.execute_at.isoformat(),
                "retry_delay_hours": action.retry_delay_hours,
                "propensity_score": action.propensity_score,
            }
            _persist_and_audit(session, record, action, exec_result)
            return {"action": action.model_dump(mode="json"), "execution": exec_result}

        # LOG_PROMISE_TO_PAY from router (rare) — also write promises row if date present
        if action.action == ActionType.LOG_PROMISE_TO_PAY and record.promised_payment_date:
            session.add(PromiseToPay(
                record_id=record.record_id,
                promised_date=record.promised_payment_date,
                promised_amount_inr=record.amount_inr,
                contact_name=record.customer_name,
                status="pending",
                notes="Auto-logged via LOG_PROMISE_TO_PAY action",
                created_at=datetime.now(timezone.utc),
            ))

        try:
            exec_result = execute_action(action, record, client=client)
        except Exception as e:
            err_str = str(e)
            outcome = "rate_limited" if "Too many requests" in err_str else "api_error"
            exec_result = {
                "record_id": action.record_id,
                "action": action.action.value,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "mock_mode": client.mock,
                "api_response": None,
                "outcome": outcome,
                "error": err_str,
            }

        _persist_and_audit(session, record, action, exec_result)

    return {
        "action": action.model_dump(mode="json"),
        "execution": exec_result,
    }


# ============================================================
# POST /process-failure
# ============================================================
@app.post("/process-failure", summary="Process a single failed transaction")
def process_failure(
    record: TransactionFailure,
    _auth: None = Depends(verify_api_key),
) -> dict[str, Any]:
    return _run_pipeline(record)


# ============================================================
# POST /webhooks/razorpay — signature auth, not API key
# ============================================================
@app.post("/webhooks/razorpay", summary="Receive Razorpay webhooks (signature-verified)")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None, alias="X-Razorpay-Signature"),
) -> dict[str, Any]:
    """
    Auth: X-Razorpay-Signature (HMAC-SHA256). Does NOT require X-Api-Key —
    Razorpay never sends our API key.

    Handled events:
      payment.failed          → recovery pipeline
      payment_link.expired / order.abandoned → checkout recovery
      payment.captured / payment_link.paid / order.paid → measured recovery
    """
    body = await request.body()
    if not verify_razorpay_signature(body, x_razorpay_signature):
        raise HTTPException(status_code=401, detail="Invalid Razorpay webhook signature.")

    import json
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type = payload.get("event") or "unknown"
    event_id = payload.get("id")

    with get_session() as session:
        is_new = check_and_mark_event(session, event_id, event_type)
        if not is_new:
            return {"status": "duplicate", "event": event_type, "event_id": event_id}

    # --- Closed-loop recovery attribution (The Bar) ---
    if event_type == "payment.captured":
        with get_session() as session:
            try:
                result = attribute_payment_captured(session, payload)
                mark_event_outcome(session, event_id, "recovered")
                return {"status": "recovered", "event": event_type, **result}
            except ValueError as e:
                return {"status": "ignored", "event": event_type, "detail": str(e)}

    if event_type == "payment_link.paid":
        with get_session() as session:
            try:
                result = attribute_payment_link_paid(session, payload)
                mark_event_outcome(session, event_id, "recovered")
                return {"status": "recovered", "event": event_type, **result}
            except ValueError as e:
                return {"status": "ignored", "event": event_type, "detail": str(e)}

    if event_type == "order.paid":
        # Prefer attribution (checkout closed) over re-running abandonment pipeline
        with get_session() as session:
            try:
                result = attribute_order_paid(session, payload)
                mark_event_outcome(session, event_id, "recovered")
                return {"status": "recovered", "event": event_type, **result}
            except ValueError:
                # Fall through to checkout mapper only if amount unpaid / partial
                pass
        try:
            record = map_checkout_event(payload)
        except ValueError as e:
            return {"status": "ignored", "event": event_type, "detail": str(e)}
        result = _run_pipeline(record)
        return {"status": "processed", "event": event_type, "record_id": record.record_id, **result}

    # --- Checkout abandonment ---
    checkout_events = {"payment_link.expired", "order.abandoned"}
    if event_type in checkout_events:
        try:
            record = map_checkout_event(payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Cannot map checkout event: {e}")
        result = _run_pipeline(record)
        return {"status": "processed", "event": event_type, "record_id": record.record_id, **result}

    # --- Payment failure ---
    if event_type != "payment.failed":
        return {"status": "ignored", "event": event_type}

    try:
        record = map_webhook_to_failure(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Cannot map webhook: {e}")

    result = _run_pipeline(record)
    return {"status": "processed", "event": event_type, "record_id": record.record_id, **result}


# ============================================================
# Promise-to-Pay Tracker
# ============================================================

class PromiseRequest(BaseModel):
    record_id: str
    promised_date: str
    promised_amount_inr: int
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None


class PromisePatchRequest(BaseModel):
    status: str
    notes: Optional[str] = None


@app.post("/promises", summary="Log a customer promise-to-pay commitment")
def log_promise(
    req: PromiseRequest,
    _auth: None = Depends(verify_api_key),
) -> dict:
    try:
        promised = date_type.fromisoformat(req.promised_date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid promised_date: {req.promised_date}")

    with get_session() as session:
        txn = session.get(Transaction, req.record_id)
        if txn is None:
            raise HTTPException(
                status_code=404,
                detail=f"record_id={req.record_id} not found in transactions table.",
            )
        promise = PromiseToPay(
            record_id=req.record_id,
            promised_date=promised,
            promised_amount_inr=req.promised_amount_inr,
            contact_name=req.contact_name,
            contact_phone=req.contact_phone,
            status="pending",
            notes=req.notes,
            created_at=datetime.now(timezone.utc),
        )
        session.add(promise)
        session.flush()
        promise_id = promise.id

        # Emit LOG_PROMISE_TO_PAY into audit trail (router action wired)
        action = AgentAction(
            record_id=req.record_id,
            action=ActionType.LOG_PROMISE_TO_PAY,
            reasoning=(
                f"Collections agent logged promise-to-pay of ₹{req.promised_amount_inr} "
                f"by {req.promised_date}."
            ),
            stop_after_this=False,
            follow_up_date=promised,
            message_language=txn.customer_language_pref or "en",
        )
        record = TransactionFailure(
            record_id=txn.record_id,
            transaction_type=TransactionType(txn.transaction_type),
            failure_reason=FailureReason(txn.failure_reason),
            amount_inr=req.promised_amount_inr,
            attempt_count=txn.attempt_count,
            last_attempt_date=txn.last_attempt_date,
            customer_name=txn.customer_name,
            customer_language_pref=txn.customer_language_pref or "en",
            past_recovery_success=txn.past_recovery_success,
            subscription_id=txn.subscription_id,
            invoice_id=txn.invoice_id,
            due_date=txn.due_date,
            promised_payment_date=promised,
        )
        exec_result = execute_action(action, record, client=_get_client())
        audit = AuditLog(
            timestamp=datetime.now(timezone.utc),
            record_id=req.record_id,
            transaction_type=txn.transaction_type,
            failure_reason=txn.failure_reason,
            amount_inr=req.promised_amount_inr,
            attempt_count=txn.attempt_count,
            action_taken=ActionType.LOG_PROMISE_TO_PAY.value,
            reasoning=action.reasoning,
            stop_after_this=False,
            execution=exec_result,
        )
        session.add(audit)
        txn.promised_payment_date = promised

    return {
        "status": "created",
        "promise_id": promise_id,
        "record_id": req.record_id,
        "promised_date": req.promised_date,
        "promised_amount_inr": req.promised_amount_inr,
        "action_logged": ActionType.LOG_PROMISE_TO_PAY.value,
    }


@app.get("/promises/overdue", summary="List broken promises; auto-route follow-ups")
def get_overdue_promises(
    auto_route: bool = True,
    _auth: None = Depends(verify_api_key),
) -> dict:
    """
    Marks overdue pending promises as broken. When auto_route=true (default),
    each broken promise is fed back through the pipeline as PROMISE_BROKEN
    (voice / high-priority escalate).
    """
    today = date_type.today()
    follow_ups = []

    with get_session() as session:
        overdue = (
            session.query(PromiseToPay)
            .filter(
                PromiseToPay.promised_date < today,
                PromiseToPay.status == "pending",
            )
            .all()
        )
        broken_payloads = []
        for p in overdue:
            p.status = "broken"
            p.updated_at = datetime.now(timezone.utc)
            txn = session.get(Transaction, p.record_id)
            broken_payloads.append({
                "promise_id": p.id,
                "record_id": p.record_id,
                "promised_date": p.promised_date.isoformat(),
                "promised_amount_inr": p.promised_amount_inr,
                "contact_name": p.contact_name,
                "contact_phone": p.contact_phone,
                "days_overdue": (today - p.promised_date).days,
                "status": "broken",
                "txn": txn,
            })

        results = [
            {k: v for k, v in bp.items() if k != "txn"}
            for bp in broken_payloads
        ]

    if auto_route:
        for bp in broken_payloads:
            txn = bp["txn"]
            if txn is None:
                continue
            try:
                record = TransactionFailure(
                    record_id=txn.record_id,
                    transaction_type=TransactionType(txn.transaction_type),
                    failure_reason=FailureReason.PROMISE_BROKEN,
                    amount_inr=bp["promised_amount_inr"],
                    attempt_count=txn.attempt_count,
                    last_attempt_date=txn.last_attempt_date,
                    customer_name=txn.customer_name,
                    customer_language_pref=txn.customer_language_pref or "hinglish",
                    past_recovery_success=txn.past_recovery_success,
                    subscription_id=txn.subscription_id,
                    invoice_id=txn.invoice_id,
                    due_date=txn.due_date,
                    promised_payment_date=date_type.fromisoformat(bp["promised_date"]),
                )
                follow_ups.append({
                    "promise_id": bp["promise_id"],
                    "pipeline": _run_pipeline(record),
                })
            except Exception as e:
                follow_ups.append({
                    "promise_id": bp["promise_id"],
                    "error": str(e),
                })

    return {
        "broken_promise_count": len(results),
        "total_at_risk_inr": sum(r["promised_amount_inr"] for r in results),
        "promises": results,
        "broken_promises": results,  # dashboard compat
        "follow_ups": follow_ups,
    }


@app.get("/promises/reminders", summary="Promises due within 24 hours — send reminders")
def get_promise_reminders(
    _auth: None = Depends(verify_api_key),
) -> dict:
    """Surface promises whose promised_date is tomorrow (or today) for reminder outreach."""
    today = date_type.today()
    tomorrow = today + timedelta(days=1)

    with get_session() as session:
        upcoming = (
            session.query(PromiseToPay)
            .filter(
                PromiseToPay.status == "pending",
                PromiseToPay.promised_date <= tomorrow,
                PromiseToPay.promised_date >= today,
            )
            .all()
        )
        reminders = []
        for p in upcoming:
            txn = session.get(Transaction, p.record_id)
            lang = (txn.customer_language_pref if txn else "hinglish") or "hinglish"
            from message_generator import get_message
            msg = get_message(
                "LOG_PROMISE_TO_PAY",
                lang,
                p.contact_name or (txn.customer_name if txn else "Customer"),
                p.promised_amount_inr,
                follow_up_date=p.promised_date.isoformat(),
            )
            reminders.append({
                "promise_id": p.id,
                "record_id": p.record_id,
                "promised_date": p.promised_date.isoformat(),
                "promised_amount_inr": p.promised_amount_inr,
                "reminder_message": msg,
                "hours_until_due": int(
                    (datetime.combine(p.promised_date, datetime.min.time())
                     - datetime.now()).total_seconds() / 3600
                ),
            })

    return {"reminder_count": len(reminders), "reminders": reminders}


@app.patch("/promises/{promise_id}", summary="Update promise status")
def update_promise(
    promise_id: int,
    req: PromisePatchRequest,
    _auth: None = Depends(verify_api_key),
) -> dict:
    valid_statuses = {"fulfilled", "partial", "broken", "pending"}
    if req.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of {valid_statuses}")

    with get_session() as session:
        promise = session.get(PromiseToPay, promise_id)
        if promise is None:
            raise HTTPException(status_code=404, detail=f"Promise {promise_id} not found")
        promise.status = req.status
        promise.notes = req.notes or promise.notes
        promise.updated_at = datetime.now(timezone.utc)

        if req.status == "fulfilled":
            txn = session.get(Transaction, promise.record_id)
            if txn:
                txn.recovery_status = "recovered"
                txn.recovered_amount_inr = promise.promised_amount_inr
                txn.recovered_at = datetime.now(timezone.utc)
                txn.past_recovery_success = True

    return {"status": "updated", "promise_id": promise_id, "new_status": req.status}


# ============================================================
# Measured recovery metrics (The Bar)
# ============================================================
@app.get("/metrics/recovery", summary="Batch-level measured ₹ recovered")
def metrics_recovery(_auth: None = Depends(verify_api_key)) -> dict:
    with get_session() as session:
        return get_batch_recovery_metrics(session)


# ============================================================
# Scheduler
# ============================================================
@app.post("/scheduler/run", summary="Execute due deferred retries and list B2B dunning candidates")
def scheduler_run(
    limit: int = 50,
    _auth: None = Depends(verify_api_key),
) -> dict:
    client = _get_client()

    def _exec(action: AgentAction, record: TransactionFailure):
        result = execute_action(action, record, client=client)
        with get_session() as session:
            _persist_and_audit(session, record, action, result)
        return result

    with get_session() as session:
        due_result = process_due_actions(session, _exec, limit=limit)
        b2b = schedule_b2b_dunning_sweep(session)

    # Re-process B2B candidates that need aging (route fresh)
    b2b_processed = []
    for cand in b2b["candidates"][:limit]:
        with get_session() as session:
            txn = session.get(Transaction, cand["record_id"])
            if txn is None:
                continue
            try:
                record = TransactionFailure(
                    record_id=txn.record_id,
                    transaction_type=TransactionType(txn.transaction_type),
                    failure_reason=FailureReason(txn.failure_reason),
                    amount_inr=txn.amount_inr,
                    attempt_count=txn.attempt_count,
                    last_attempt_date=txn.last_attempt_date,
                    customer_name=txn.customer_name,
                    customer_language_pref=txn.customer_language_pref or "en",
                    past_recovery_success=txn.past_recovery_success,
                    invoice_id=txn.invoice_id,
                    due_date=txn.due_date,
                    po_reference=txn.po_reference,
                    counterparty_business_name=txn.counterparty_business_name,
                )
            except Exception:
                continue
        # Only re-route if due_date aging crossed a tier since last attempt
        # (simple: always allow one dunning pass per scheduler run if not recovered)
        pipe = _run_pipeline(record)
        b2b_processed.append({"record_id": cand["record_id"], "pipeline": pipe})

    return {
        "deferred_retries": due_result,
        "b2b_dunning": {"candidates": b2b["count"], "processed": b2b_processed},
    }


@app.get("/scheduler/pending", summary="List pending scheduled actions")
def scheduler_pending(_auth: None = Depends(verify_api_key)) -> dict:
    with get_session() as session:
        rows = (
            session.query(PendingAction)
            .filter(PendingAction.status == "pending")
            .order_by(PendingAction.execute_at.asc())
            .limit(100)
            .all()
        )
        return {
            "count": len(rows),
            "pending": [
                {
                    "id": r.id,
                    "record_id": r.record_id,
                    "action_type": r.action_type,
                    "execute_at": r.execute_at.isoformat(),
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }


# ============================================================
# Degradation detection
# ============================================================
@app.get("/analyze/degradation", summary="Detect bank/issuer outage patterns")
def analyze_degradation(
    hours: int = 1,
    auto_pause: bool = False,
    _auth: None = Depends(verify_api_key),
) -> dict:
    from sqlalchemy import text

    hours = min(max(1, hours), 72)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    with get_session() as session:
        rows = session.execute(text("""
            SELECT failure_reason,
                   COUNT(*)           AS cnt,
                   SUM(amount_inr)    AS total_inr
            FROM audit_log
            WHERE timestamp >= :cutoff
            GROUP BY failure_reason
            ORDER BY cnt DESC
        """), {"cutoff": cutoff}).fetchall()

        total_failures = sum(r[1] for r in rows)
        clusters = []
        pause_applied = False
        for reason, cnt, total_inr in rows:
            rate = round(cnt / total_failures, 2) if total_failures else 0
            recommendation = _degradation_recommendation(reason, rate, cnt)
            if recommendation:
                clusters.append({
                    "failure_reason": reason,
                    "count": cnt,
                    "total_inr": total_inr,
                    "rate": rate,
                    "pct": rate,  # dashboard compat
                    "recommendation": recommendation,
                })
                # Auto-pause: cancel pending RETRY_PAYMENT during bank outage
                if auto_pause and reason == "bank_downtime" and cnt >= 2:
                    cancelled = (
                        session.query(PendingAction)
                        .filter(
                            PendingAction.status == "pending",
                            PendingAction.action_type == "RETRY_PAYMENT",
                        )
                        .all()
                    )
                    for p in cancelled:
                        p.status = "cancelled"
                        p.result = {"reason": "degradation_auto_pause", "cluster": reason}
                    pause_applied = len(cancelled) > 0

    return {
        "window_hours": hours,
        "total_failures": total_failures,
        "degradation_detected": len(clusters) > 0,
        "clusters": clusters,
        "auto_pause_applied": pause_applied,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def _degradation_recommendation(reason: str, rate: float, count: int) -> str | None:
    if reason == "bank_downtime" and count >= 2:
        return (
            f"Suspected bank outage ({count} failures). "
            "Pause retries, wait 2-4h before re-processing. "
            "Call GET /analyze/degradation?auto_pause=true to cancel pending retries."
        )
    if reason == "3ds_auth_fail" and rate >= 0.20:
        return f"High 3DS failure rate ({rate:.0%}). Possible card network issue — check with issuer."
    if reason == "bank_decline" and rate >= 0.30:
        return f"High bank decline rate ({rate:.0%}). Consider switching payment gateway or alerting ops."
    if reason == "hard_decline" and count >= 3:
        return f"{count} hard declines detected. Review for potential fraud cluster."
    return None


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "name": "Razorpay Revenue Recovery Agent API",
        "version": "5.0.0",
        "docs": "http://127.0.0.1:8000/docs",
        "health": "http://127.0.0.1:8000/health",
        "status": "running",
        "message": "Welcome to the Revenue Recovery API. Interactive OpenAPI docs available at /docs.",
    }


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "version": "5.0.0"}


# ==========================================================================
# Voice Routes — Hinglish Voice Recovery Agent (Blueprint Track 03)
# ==========================================================================

from voice_agent import (
    get_assistant_config,
    create_web_call,
    get_call_status,
    BACKEND_BASE_URL,
)


class VoiceLaunchRequest(BaseModel):
    record_id: str
    customer_name: str
    amount_inr: int
    failure_reason: str = ""
    backend_url: Optional[str] = None


class VoicePtpRequest(BaseModel):
    record_id: str
    agreed_date: str          # ISO date YYYY-MM-DD
    contact_name: Optional[str] = None
    notes: Optional[str] = None


class VoiceOptOutRequest(BaseModel):
    record_id: str
    reason: Optional[str] = "customer_request"


class VoiceEscalateRequest(BaseModel):
    record_id: str
    issue_type: str = "other"
    summary: Optional[str] = None


class VoiceLinkRequest(BaseModel):
    record_id: str
    method: Optional[str] = "link_only"


# --------------------------------------------------------------------------
# GET /voice/agent-config
# --------------------------------------------------------------------------

@app.get("/voice/agent-config")
def voice_agent_config(
    record_id: str = "",
    customer_name: str = "Customer",
    amount_inr: int = 0,
    failure_reason: str = "",
    _: str = Depends(verify_api_key),
) -> dict:
    """
    Returns the Vapi assistant JSON config for this transaction.
    Use this to create/update an assistant in the Vapi dashboard, or
    pass it directly as the `assistant` field in a POST /call request.
    """
    return get_assistant_config(
        backend_url=BACKEND_BASE_URL,
        customer_name=customer_name,
        amount_inr=amount_inr,
        record_id=record_id,
        failure_reason=failure_reason,
    )


# --------------------------------------------------------------------------
# POST /voice/call/launch  — create a Vapi Web Call (browser WebRTC)
# --------------------------------------------------------------------------

@app.post("/voice/call/launch")
def voice_launch_call(
    body: VoiceLaunchRequest,
    _: str = Depends(verify_api_key),
) -> dict:
    """
    Launch a Vapi Web Call for the given transaction.
    Returns { call_id, web_call_url, status, mock }.
    Open web_call_url in the browser to start the live Hinglish voice session.
    If VAPI_API_KEY is not set, returns a mock call for demo purposes.
    """
    now = datetime.now(timezone.utc)

    call_result = create_web_call(
        record_id=body.record_id,
        amount_inr=body.amount_inr,
        customer_name=body.customer_name,
        failure_reason=body.failure_reason,
        backend_url=body.backend_url or BACKEND_BASE_URL,
    )

    # Persist to VoiceCall table
    with get_session() as session:
        from message_generator import generate_voice_script
        voice = generate_voice_script(
            language="hinglish",
            customer_name=body.customer_name,
            amount_inr=body.amount_inr,
            reason=body.failure_reason,
        )
        vc = VoiceCall(
            call_id=call_result.get("call_id") or f"call_err_{now.timestamp():.0f}",
            record_id=body.record_id,
            status=call_result.get("status", "initiated"),
            language="hinglish",
            voice_script=voice["script"],
            mock=call_result.get("mock", False),
            created_at=now,
        )
        session.add(vc)

    return call_result


# --------------------------------------------------------------------------
# GET /voice/calls  — list all VoiceCall records
# --------------------------------------------------------------------------

@app.get("/voice/calls")
def list_voice_calls(
    limit: int = 50,
    _: str = Depends(verify_api_key),
) -> list[dict]:
    """List recent VoiceCall records from the DB."""
    with get_session() as session:
        rows = (
            session.query(VoiceCall)
            .order_by(VoiceCall.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "call_id": r.call_id,
                "record_id": r.record_id,
                "status": r.status,
                "outcome": r.outcome,
                "language": r.language,
                "mock": r.mock,
                "duration_sec": r.duration_sec,
                "payment_link_url": r.payment_link_url,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in rows
        ]


# --------------------------------------------------------------------------
# GET /voice/calls/{call_id}  — live status from Vapi
# --------------------------------------------------------------------------

@app.get("/voice/calls/{call_id}")
def get_voice_call(
    call_id: str,
    _: str = Depends(verify_api_key),
) -> dict:
    """Fetch call status and transcript from Vapi (live) and merge with DB record."""
    live = get_call_status(call_id)

    with get_session() as session:
        vc = session.query(VoiceCall).filter(VoiceCall.call_id == call_id).first()
        if vc is None:
            return live
        db_data = {
            "record_id": vc.record_id,
            "outcome": vc.outcome,
            "language": vc.language,
            "voice_script": vc.voice_script,
            "payment_link_url": vc.payment_link_url,
            "mock": vc.mock,
            "duration_sec": vc.duration_sec,
            "created_at": vc.created_at.isoformat() if vc.created_at else None,
        }
    live.update(db_data)
    return live


# --------------------------------------------------------------------------
# POST /voice/inbound  — Vapi server-URL webhook
# Receives all Vapi events: assistant-request, transcript, end-of-call-report
# --------------------------------------------------------------------------

@app.post("/voice/inbound", include_in_schema=False)
async def voice_inbound_webhook(request: Request) -> dict:
    """
    Vapi server-URL webhook. Handles:
      - end-of-call-report: update VoiceCall status/transcript/duration
      - transcript: streaming transcript chunks (ignored for now, logged)
      - assistant-request: (not used in web-call mode)
    """
    try:
        payload = await request.json()
    except Exception:
        return {"result": "ignored", "reason": "invalid_json"}

    msg_type = payload.get("message", {}).get("type") or payload.get("type", "")
    call_data = payload.get("message", {}).get("call") or payload.get("call") or {}
    call_id = call_data.get("id") or payload.get("callId", "")

    if msg_type == "end-of-call-report" or msg_type == "end_of_call_report":
        # Full transcript + outcome summary
        artifact = payload.get("message", {}).get("artifact") or payload.get("artifact") or {}
        transcript_arr = artifact.get("messages") or artifact.get("transcript") or []
        duration = call_data.get("endedAt") and call_data.get("startedAt") and None  # calculated below
        ended_at_str = call_data.get("endedAt") or payload.get("message", {}).get("endedAt")
        started_at_str = call_data.get("startedAt") or payload.get("message", {}).get("startedAt")
        dur_sec = None
        try:
            if ended_at_str and started_at_str:
                from datetime import timezone as tz
                e = datetime.fromisoformat(ended_at_str.replace("Z", "+00:00"))
                s = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                dur_sec = max(0, int((e - s).total_seconds()))
        except Exception:
            pass

        with get_session() as session:
            vc = session.query(VoiceCall).filter(VoiceCall.call_id == call_id).first()
            if vc:
                vc.status = "completed"
                vc.transcript = transcript_arr
                vc.duration_sec = dur_sec
                vc.completed_at = datetime.now(timezone.utc)
                vc.raw_payload = payload

        return {"result": "end_of_call_logged", "call_id": call_id}

    # For all other event types, just acknowledge
    return {"result": "acknowledged", "type": msg_type}


# --------------------------------------------------------------------------
# POST /voice/tools/send_payment_link  — Vapi tool call
# --------------------------------------------------------------------------

@app.post("/voice/tools/send_payment_link")
async def voice_tool_send_payment_link(request: Request) -> dict:
    """
    Vapi calls this when the agent decides to send a payment link during a call.
    Creates a real Razorpay payment link and returns the URL to the agent.
    The agent reads the URL aloud (or confirms link is sent).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid request body")

    # Vapi wraps tool args in message.toolCalls[].function.arguments
    args: dict = {}
    call_id = ""
    tool_call_id = ""
    try:
        msg = body.get("message", body)
        tool_calls = msg.get("toolCalls") or msg.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            tool_call_id = tc.get("id", "")
            import json as _json
            raw_args = tc.get("function", {}).get("arguments", "{}")
            args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        call_id = msg.get("call", {}).get("id", "")
    except Exception:
        args = body  # fallback: treat body directly as args

    record_id = args.get("record_id", "")
    if not record_id:
        return {"results": [{"toolCallId": tool_call_id, "result": "Error: record_id is required."}]}

    # Look up the transaction
    with get_session() as session:
        txn = session.get(Transaction, record_id)
        if txn is None:
            return {"results": [{"toolCallId": tool_call_id,
                                  "result": f"Error: Transaction {record_id} not found."}]}
        amount_inr = txn.amount_inr
        customer_name = txn.customer_name
        language = txn.customer_language_pref or "hinglish"

    # Create the Razorpay payment link
    from message_generator import generate_message
    description = generate_message(
        action_type="SEND_PAYMENT_LINK",
        language=language,
        customer_name=customer_name,
        amount_inr=amount_inr,
    )
    client = _get_client()
    api_result = client.create_payment_link(
        record_id=record_id,
        amount_inr=amount_inr,
        description=description,
        customer_name=customer_name,
    )
    short_url = api_result.get("short_url") or api_result.get("id", "")

    # Update VoiceCall record
    with get_session() as session:
        vc = session.query(VoiceCall).filter(VoiceCall.call_id == call_id).first()
        if vc:
            vc.outcome = "link_sent"
            vc.payment_link_url = short_url

    result_text = (
        f"Payment link created: {short_url}. "
        f"Please tell the customer: 'Maine aapko link bhej diya hai — "
        f"WhatsApp ya SMS check karein aur Rs.{amount_inr:,} pay karein. Dhanyavaad!'"
    )
    return {"results": [{"toolCallId": tool_call_id, "result": result_text}]}


# --------------------------------------------------------------------------
# POST /voice/tools/schedule_ptp_retry  — Vapi tool call
# --------------------------------------------------------------------------

@app.post("/voice/tools/schedule_ptp_retry")
async def voice_tool_schedule_ptp(request: Request) -> dict:
    """Record a Promise-to-Pay commitment made during a voice call."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid request body")

    args: dict = {}
    tool_call_id = ""
    call_id = ""
    try:
        msg = body.get("message", body)
        tool_calls = msg.get("toolCalls") or msg.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            tool_call_id = tc.get("id", "")
            import json as _json
            raw_args = tc.get("function", {}).get("arguments", "{}")
            args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        call_id = msg.get("call", {}).get("id", "")
    except Exception:
        args = body

    record_id = args.get("record_id", "")
    agreed_date_str = args.get("agreed_date", "")

    if not record_id or not agreed_date_str:
        return {"results": [{"toolCallId": tool_call_id,
                              "result": "Error: record_id and agreed_date are required."}]}

    from datetime import date as date_type
    try:
        agreed_date = date_type.fromisoformat(agreed_date_str)
    except ValueError:
        return {"results": [{"toolCallId": tool_call_id,
                              "result": f"Error: Invalid date format '{agreed_date_str}'. Use YYYY-MM-DD."}]}

    now = datetime.now(timezone.utc)
    with get_session() as session:
        txn = session.get(Transaction, record_id)
        if txn is None:
            return {"results": [{"toolCallId": tool_call_id,
                                  "result": f"Error: Transaction {record_id} not found."}]}
        amount_inr = txn.amount_inr

        promise = PromiseToPay(
            record_id=record_id,
            promised_date=agreed_date,
            promised_amount_inr=amount_inr,
            contact_name=args.get("contact_name") or txn.customer_name,
            status="pending",
            notes=args.get("notes") or "Promised during voice call (Riya)",
            created_at=now,
        )
        session.add(promise)

        # Update VoiceCall outcome
        vc = session.query(VoiceCall).filter(VoiceCall.call_id == call_id).first()
        if vc:
            vc.outcome = "ptp_scheduled"

    result_text = (
        f"Promise-to-Pay recorded for Rs.{amount_inr:,} by {agreed_date_str}. "
        f"Tell the customer: 'Aapki commitment note ho gayi hai — "
        f"{agreed_date_str} ko hum phir try karenge. Shukriya!'"
    )
    return {"results": [{"toolCallId": tool_call_id, "result": result_text}]}


# --------------------------------------------------------------------------
# POST /voice/tools/mark_opt_out  — Vapi tool call
# --------------------------------------------------------------------------

@app.post("/voice/tools/mark_opt_out")
async def voice_tool_opt_out(request: Request) -> dict:
    """Flag a customer as DND — stop all future automated outreach."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid request body")

    args: dict = {}
    tool_call_id = ""
    call_id = ""
    try:
        msg = body.get("message", body)
        tool_calls = msg.get("toolCalls") or msg.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            tool_call_id = tc.get("id", "")
            import json as _json
            raw_args = tc.get("function", {}).get("arguments", "{}")
            args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        call_id = msg.get("call", {}).get("id", "")
    except Exception:
        args = body

    record_id = args.get("record_id", "")
    if not record_id:
        return {"results": [{"toolCallId": tool_call_id, "result": "Error: record_id is required."}]}

    with get_session() as session:
        txn = session.get(Transaction, record_id)
        if txn:
            txn.recovery_status = "opted_out"
        vc = session.query(VoiceCall).filter(VoiceCall.call_id == call_id).first()
        if vc:
            vc.outcome = "opted_out"

    result_text = (
        "Customer opted out — DND flag set. All future automated outreach halted. "
        "Say: 'Bilkul ji, hum dobara call nahi karenge. Agar kabhi zaroorat ho to "
        "aap humse contact kar saktein hain. Dhanyavaad aur maafi chahta hoon.'"
    )
    return {"results": [{"toolCallId": tool_call_id, "result": result_text}]}


# --------------------------------------------------------------------------
# POST /voice/tools/escalate_to_human  — Vapi tool call
# --------------------------------------------------------------------------

@app.post("/voice/tools/escalate_to_human")
async def voice_tool_escalate(request: Request) -> dict:
    """Route customer to human support — log audit entry and queue for human follow-up."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid request body")

    args: dict = {}
    tool_call_id = ""
    call_id = ""
    try:
        msg = body.get("message", body)
        tool_calls = msg.get("toolCalls") or msg.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            tool_call_id = tc.get("id", "")
            import json as _json
            raw_args = tc.get("function", {}).get("arguments", "{}")
            args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        call_id = msg.get("call", {}).get("id", "")
    except Exception:
        args = body

    record_id = args.get("record_id", "")
    issue_type = args.get("issue_type", "other")
    summary = args.get("summary", "")

    if not record_id:
        return {"results": [{"toolCallId": tool_call_id, "result": "Error: record_id is required."}]}

    now = datetime.now(timezone.utc)
    with get_session() as session:
        txn = session.get(Transaction, record_id)
        if txn:
            txn.recovery_status = "escalated"
        # Write audit log entry
        al = AuditLog(
            timestamp=now,
            record_id=record_id,
            transaction_type=txn.transaction_type if txn else "unknown",
            failure_reason=txn.failure_reason if txn else "unknown",
            amount_inr=txn.amount_inr if txn else 0,
            attempt_count=txn.attempt_count if txn else 0,
            action_taken="ESCALATE_TO_HUMAN",
            reasoning=f"Voice call escalation — issue: {issue_type}. {summary}",
            stop_after_this=True,
            execution={"source": "voice_call", "call_id": call_id,
                       "issue_type": issue_type, "summary": summary},
        )
        session.add(al)
        vc = session.query(VoiceCall).filter(VoiceCall.call_id == call_id).first()
        if vc:
            vc.outcome = "escalated"

    result_text = (
        f"Escalation queued for issue: {issue_type}. "
        "Tell the customer: 'Aapki problem note ho gayi hai — hamaari team "
        "24 ghante mein aapse contact karegi. Aapka shukriya aur sorry for the inconvenience.'"
    )
    return {"results": [{"toolCallId": tool_call_id, "result": result_text}]}


# --------------------------------------------------------------------------
# Sarvam AI Voice Routes (TTS & Hinglish Audio Generation)
# --------------------------------------------------------------------------

import sarvam_tts

os.makedirs("data/audio", exist_ok=True)
app.mount("/audio", StaticFiles(directory="data/audio"), name="audio")


class SarvamTTSRequest(BaseModel):
    text: str
    speaker: str = "priya"
    model: str = "bulbul:v3"
    language_code: str = "hi-IN"


class SarvamScriptRequest(BaseModel):
    record_id: str
    speaker: str = "priya"


@app.get("/voice/sarvam/voices")
async def voice_sarvam_voices() -> dict:
    """List available Sarvam AI voices."""
    return {"voices": sarvam_tts.list_voices()}


@app.post("/voice/sarvam/tts")
async def voice_sarvam_tts(payload: SarvamTTSRequest) -> dict:
    """Generate audio from arbitrary text using Sarvam TTS."""
    res = sarvam_tts.generate_hinglish_audio(
        text=payload.text,
        speaker=payload.speaker,
        model=payload.model,
        language_code=payload.language_code,
    )
    return res


@app.post("/voice/sarvam/generate-script-audio")
async def voice_sarvam_generate_script(payload: SarvamScriptRequest) -> dict:
    """
    Generate recovery call script and Sarvam Hinglish audio for a transaction.
    """
    with get_session() as session:
        txn = session.get(Transaction, payload.record_id)
        if not txn:
            raise HTTPException(404, f"Transaction {payload.record_id} not found")
        customer_name = txn.customer_name or "Valued Customer"
        amount_inr = txn.amount_inr or 0
        failure_reason = txn.failure_reason or "bank_decline"

    res = sarvam_tts.generate_recovery_audio(
        customer_name=customer_name,
        amount_inr=amount_inr,
        failure_reason=failure_reason,
        speaker=payload.speaker,
    )
    return res


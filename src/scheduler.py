"""
scheduler.py — Deferred recovery action queue.

Enforces retry_delay_hours and B2B dunning cadence by persisting PendingAction
rows and executing them when execute_at <= now.

Usage:
  - schedule_action() after routing when RETRY_PAYMENT has delay > 0
  - process_due_actions() via POST /scheduler/run or cron
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from db.models import PendingAction, Transaction
from schemas import TransactionFailure, AgentAction, ActionType


def schedule_action(
    session: Session,
    record: TransactionFailure,
    action: AgentAction,
    delay_hours: Optional[int] = None,
) -> PendingAction:
    """Enqueue a deferred action. delay_hours defaults to action.retry_delay_hours."""
    hours = delay_hours if delay_hours is not None else (action.retry_delay_hours or 0)
    now = datetime.now(timezone.utc)
    execute_at = now + timedelta(hours=max(0, hours))

    # Ensure transaction row exists for FK
    if session.get(Transaction, record.record_id) is None:
        _TXN_COLUMNS = {c.key for c in Transaction.__table__.columns}
        txn_data = {k: v for k, v in record.model_dump().items() if k in _TXN_COLUMNS}
        txn_data["transaction_type"] = record.transaction_type.value
        txn_data["failure_reason"] = record.failure_reason.value
        txn_data.setdefault("recovery_status", "pending")
        session.add(Transaction(**txn_data))
        session.flush()

    pending = PendingAction(
        record_id=record.record_id,
        action_type=action.action.value,
        action_payload=action.model_dump(mode="json"),
        record_payload=record.model_dump(mode="json"),
        execute_at=execute_at,
        status="pending",
        created_at=now,
    )
    session.add(pending)
    session.flush()
    return pending


def list_due_actions(session: Session, limit: int = 50) -> list[PendingAction]:
    now = datetime.now(timezone.utc)
    return (
        session.query(PendingAction)
        .filter(
            PendingAction.status == "pending",
            PendingAction.execute_at <= now,
        )
        .order_by(PendingAction.execute_at.asc())
        .limit(limit)
        .all()
    )


def process_due_actions(
    session: Session,
    execute_fn,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Execute all due pending actions via execute_fn(action, record) -> exec_result.
    Marks each pending row executed/failed.
    """
    due = list_due_actions(session, limit=limit)
    results = []
    for pending in due:
        try:
            record = TransactionFailure(**pending.record_payload)
            action = AgentAction(**pending.action_payload)
            # Clear defer flag — we're executing now
            action = action.model_copy(update={"defer_execution": False})
            exec_result = execute_fn(action, record)
            pending.status = "executed"
            pending.executed_at = datetime.now(timezone.utc)
            pending.result = exec_result
            results.append({
                "pending_id": pending.id,
                "record_id": pending.record_id,
                "action": pending.action_type,
                "status": "executed",
                "execution": exec_result,
            })
        except Exception as e:
            pending.status = "failed"
            pending.executed_at = datetime.now(timezone.utc)
            pending.result = {"error": str(e)}
            results.append({
                "pending_id": pending.id,
                "record_id": pending.record_id,
                "status": "failed",
                "error": str(e),
            })

    return {
        "processed": len(results),
        "results": results,
        "run_at": datetime.now(timezone.utc).isoformat(),
    }


def schedule_b2b_dunning_sweep(session: Session) -> dict[str, Any]:
    """
    Find open B2B invoices that aren't recovered and re-queue a dunning action
    by bumping them through the router (caller should run route+schedule).
    Returns list of record_ids that need re-processing.
    """
    open_invoices = (
        session.query(Transaction)
        .filter(
            Transaction.transaction_type == "b2b_invoice",
            Transaction.recovery_status != "recovered",
        )
        .all()
    )
    return {
        "candidates": [
            {
                "record_id": t.record_id,
                "amount_inr": t.amount_inr,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "attempt_count": t.attempt_count,
            }
            for t in open_invoices
        ],
        "count": len(open_invoices),
    }

"""
recovery_attribution.py — Closed-loop recovery tracking (Track 03 The Bar).

Maps payment.captured / payment_link.paid webhooks → RecoveryEvent rows and
updates Transaction.recovery_status so the dashboard can show measured ₹ recovered
(not just "links sent").

Attribution strategy:
  1. Prefer notes.record_id on the payment / payment_link entity
  2. Fall back to reference_id (we stamp record_id-{run_suffix} on link create)
  3. Fall back to payment_link notes if nested under payment_link.paid
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from db.models import Transaction, RecoveryEvent


def _extract_record_id(entity: dict) -> Optional[str]:
    notes = entity.get("notes") or {}
    if isinstance(notes, dict) and notes.get("record_id"):
        return str(notes["record_id"])

    ref = entity.get("reference_id") or ""
    if ref:
        # Our create_payment_link uses "{record_id}-{run_suffix}"
        # Strip trailing run suffix if present (8-char alphanumeric)
        parts = str(ref).rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) <= 12:
            return parts[0]
        return str(ref)

    return None


def _amount_inr_from_entity(entity: dict) -> int:
    amount_paise = entity.get("amount") or entity.get("amount_paid") or 0
    return int(amount_paise) // 100


def attribute_payment_captured(
    session: Session,
    payload: dict,
) -> dict[str, Any]:
    """
    Process payment.captured webhook. Returns attribution result dict.
    Idempotent when razorpay event id is present (caller should also check
    ProcessedWebhook).
    """
    event_id = payload.get("id") or (payload.get("event_id"))
    try:
        entity = payload["payload"]["payment"]["entity"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Malformed payment.captured payload: {e}")

    record_id = _extract_record_id(entity)
    if not record_id:
        # Try payment_link notes via description/notes only
        raise ValueError("Cannot attribute payment.captured — no record_id in notes/reference_id")

    amount_inr = _amount_inr_from_entity(entity)
    if amount_inr <= 0:
        raise ValueError(f"Invalid recovered amount: {amount_inr}")

    payment_id = entity.get("id")
    now = datetime.now(timezone.utc)

    # Idempotency on event or payment id
    if event_id:
        existing = (
            session.query(RecoveryEvent)
            .filter(RecoveryEvent.razorpay_event_id == event_id)
            .first()
        )
        if existing:
            return {
                "status": "duplicate",
                "record_id": existing.record_id,
                "amount_inr": existing.amount_inr,
                "recovery_event_id": existing.id,
            }

    txn = session.get(Transaction, record_id)
    if txn is None:
        # Soft-create a stub so FK constraint is satisfied for late captures
        # of records that only exist in audit JSONL (batch-only path).
        txn = Transaction(
            record_id=record_id,
            transaction_type="one_time_payment",
            failure_reason="bank_decline",
            amount_inr=amount_inr,
            attempt_count=0,
            last_attempt_date=now.date(),
            customer_name="Unknown",
            customer_language_pref="en",
            past_recovery_success=False,
            recovery_status="pending",
        )
        session.add(txn)
        session.flush()

    event = RecoveryEvent(
        record_id=record_id,
        razorpay_payment_id=payment_id,
        razorpay_event_id=event_id,
        amount_inr=amount_inr,
        event_type="payment.captured",
        recovered_at=now,
        raw_payload=payload,
    )
    session.add(event)

    txn.recovery_status = "recovered"
    txn.recovered_amount_inr = amount_inr
    txn.recovered_at = now
    txn.past_recovery_success = True

    return {
        "status": "recovered",
        "record_id": record_id,
        "amount_inr": amount_inr,
        "payment_id": payment_id,
        "event_type": "payment.captured",
    }


def attribute_payment_link_paid(
    session: Session,
    payload: dict,
) -> dict[str, Any]:
    """Process payment_link.paid — customer completed a recovery payment link."""
    event_id = payload.get("id")
    try:
        entity = payload["payload"]["payment_link"]["entity"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Malformed payment_link.paid payload: {e}")

    record_id = _extract_record_id(entity)
    if not record_id:
        raise ValueError("Cannot attribute payment_link.paid — no record_id")

    amount_inr = _amount_inr_from_entity(entity)
    # Prefer amount_paid if present
    if entity.get("amount_paid"):
        amount_inr = int(entity["amount_paid"]) // 100

    if amount_inr <= 0:
        raise ValueError(f"Invalid recovered amount: {amount_inr}")

    now = datetime.now(timezone.utc)

    if event_id:
        existing = (
            session.query(RecoveryEvent)
            .filter(RecoveryEvent.razorpay_event_id == event_id)
            .first()
        )
        if existing:
            return {
                "status": "duplicate",
                "record_id": existing.record_id,
                "amount_inr": existing.amount_inr,
            }

    txn = session.get(Transaction, record_id)
    if txn is None:
        raise ValueError(f"record_id={record_id} not found — cannot attribute recovery")

    event = RecoveryEvent(
        record_id=record_id,
        razorpay_payment_id=entity.get("id"),
        razorpay_event_id=event_id,
        amount_inr=amount_inr,
        event_type="payment_link.paid",
        recovered_at=now,
        raw_payload=payload,
    )
    session.add(event)

    txn.recovery_status = "recovered"
    txn.recovered_amount_inr = amount_inr
    txn.recovered_at = now
    txn.past_recovery_success = True

    return {
        "status": "recovered",
        "record_id": record_id,
        "amount_inr": amount_inr,
        "event_type": "payment_link.paid",
    }


def attribute_order_paid(
    session: Session,
    payload: dict,
) -> dict[str, Any]:
    """
    order.paid after checkout recovery — marks CART-{order_id} or notes.record_id
    as recovered (closes checkout drop-off funnel).
    """
    event_id = payload.get("id")
    try:
        entity = payload["payload"]["order"]["entity"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Malformed order.paid payload: {e}")

    notes = entity.get("notes") or {}
    order_id = entity.get("id")
    record_id = notes.get("record_id") or (f"CART-{order_id}" if order_id else None)
    if not record_id:
        raise ValueError("Cannot attribute order.paid — no record_id")

    amount_inr = int(entity.get("amount_paid") or entity.get("amount") or 0) // 100
    if amount_inr <= 0:
        raise ValueError(f"Invalid recovered amount: {amount_inr}")

    now = datetime.now(timezone.utc)

    if event_id:
        existing = (
            session.query(RecoveryEvent)
            .filter(RecoveryEvent.razorpay_event_id == event_id)
            .first()
        )
        if existing:
            return {"status": "duplicate", "record_id": existing.record_id}

    txn = session.get(Transaction, record_id)
    if txn is None:
        # Try without CART- prefix
        alt = notes.get("record_id")
        if alt:
            txn = session.get(Transaction, alt)
            if txn:
                record_id = alt

    if txn is None:
        raise ValueError(f"record_id={record_id} not found for order.paid")

    event = RecoveryEvent(
        record_id=record_id,
        razorpay_payment_id=order_id,
        razorpay_event_id=event_id,
        amount_inr=amount_inr,
        event_type="order.paid",
        recovered_at=now,
        raw_payload=payload,
    )
    session.add(event)
    txn.recovery_status = "recovered"
    txn.recovered_amount_inr = amount_inr
    txn.recovered_at = now
    txn.past_recovery_success = True

    return {
        "status": "recovered",
        "record_id": record_id,
        "amount_inr": amount_inr,
        "event_type": "order.paid",
    }


def get_batch_recovery_metrics(session: Session) -> dict[str, Any]:
    """Aggregate measured recovery for The Bar dashboard metric."""
    from sqlalchemy import func

    at_risk = session.query(func.coalesce(func.sum(Transaction.amount_inr), 0)).scalar() or 0
    recovered_sum = (
        session.query(func.coalesce(func.sum(RecoveryEvent.amount_inr), 0)).scalar() or 0
    )
    recovered_count = session.query(func.count(RecoveryEvent.id)).scalar() or 0
    txn_count = session.query(func.count(Transaction.record_id)).scalar() or 0
    recovered_txns = (
        session.query(func.count(Transaction.record_id))
        .filter(Transaction.recovery_status == "recovered")
        .scalar()
        or 0
    )

    return {
        "total_transactions": txn_count,
        "total_at_risk_inr": int(at_risk),
        "recovered_amount_inr": int(recovered_sum),
        "recovered_event_count": int(recovered_count),
        "recovered_transaction_count": int(recovered_txns),
        "recovery_rate_pct": round(
            100 * recovered_txns / txn_count, 1
        ) if txn_count else 0.0,
        "note": (
            "recovered_amount_inr is attributed from payment.captured / "
            "payment_link.paid / order.paid webhooks — measured money, not links sent."
        ),
    }

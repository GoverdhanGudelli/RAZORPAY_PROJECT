"""
checkout_mapper.py — Maps Razorpay checkout/order events → TransactionFailure.

Handles two webhook events that signal checkout drop-off:
  1. payment_link.expired — customer received a payment link but never completed it
  2. order.paid (amount_paid < amount_due) — partial payment / abandoned

Both are converted to TransactionFailure(
    transaction_type=CHECKOUT_ABANDONMENT,
    failure_reason=CART_ABANDONED,
)
so the main router can apply urgency-tiered cart recovery logic.

Webhook shapes handled:
  payment_link.expired:
    payload.payment_link.entity.{id, amount, reference_id, notes, created_at}

  order event (simplified):
    payload.order.entity.{id, amount, amount_paid, notes, created_at}

Like webhook_mapper.py: raises ValueError on structurally invalid payloads
so the API layer can return HTTP 400 rather than silently dropping the event.
"""

from __future__ import annotations
import time
from datetime import date, datetime, timezone
from typing import Optional

from schemas import (
    TransactionFailure, TransactionType, FailureReason,
)

# If the cart has been abandoned longer than this many hours, mark as cold lead
COLD_LEAD_HOURS = 48


def _hours_since(epoch_seconds: Optional[float]) -> int:
    """Convert a Unix timestamp to hours elapsed since then (min 0)."""
    if not epoch_seconds:
        return 0
    elapsed = time.time() - float(epoch_seconds)
    return max(0, int(elapsed / 3600))


def map_payment_link_expired(payload: dict) -> TransactionFailure:
    """
    Maps a payment_link.expired webhook payload → TransactionFailure.

    Razorpay fires this when a Payment Link's expiry time passes without
    the customer completing the payment — a classic checkout abandonment signal.
    """
    try:
        entity = payload["payload"]["payment_link"]["entity"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Malformed payment_link.expired payload — missing payment_link.entity: {e}")

    amount_paise = entity.get("amount", 0)
    amount_inr = int(amount_paise) // 100
    if amount_inr <= 0:
        raise ValueError(f"payment_link amount={amount_paise} paise → ₹{amount_inr} invalid")

    notes: dict = entity.get("notes") or {}
    record_id = (
        notes.get("record_id")
        or entity.get("reference_id")
        or entity.get("id")
    )
    if not record_id:
        raise ValueError("Cannot determine record_id from payment_link.expired payload")

    hours = _hours_since(entity.get("created_at"))

    return TransactionFailure(
        record_id              = f"CART-{record_id}",
        transaction_type       = TransactionType.CHECKOUT_ABANDONMENT,
        failure_reason         = FailureReason.CART_ABANDONED,
        amount_inr             = amount_inr,
        attempt_count          = 0,
        last_attempt_date      = date.today(),
        customer_name          = notes.get("customer_name") or "Customer",
        customer_language_pref = notes.get("customer_language_pref", "hinglish"),
        payment_method         = None,
        past_recovery_success  = False,
        checkout_session_id    = entity.get("id"),
        hours_since_checkout   = hours,
    )


def map_order_abandoned(payload: dict) -> TransactionFailure:
    """
    Maps an order-based abandonment event → TransactionFailure.
    Used when an order entity is available (e.g. Razorpay Order ID tracked
    in your checkout flow) but the payment was never completed.
    """
    try:
        entity = payload["payload"]["order"]["entity"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Malformed order payload — missing order.entity: {e}")

    amount_paise = entity.get("amount", 0)
    amount_inr = int(amount_paise) // 100
    if amount_inr <= 0:
        raise ValueError(f"Order amount={amount_paise} paise → ₹{amount_inr} invalid")

    notes: dict = entity.get("notes") or {}
    record_id = notes.get("record_id") or entity.get("id")
    if not record_id:
        raise ValueError("Cannot determine record_id from order payload")

    hours = _hours_since(entity.get("created_at"))

    return TransactionFailure(
        record_id              = f"CART-{record_id}",
        transaction_type       = TransactionType.CHECKOUT_ABANDONMENT,
        failure_reason         = FailureReason.CART_ABANDONED,
        amount_inr             = amount_inr,
        attempt_count          = 0,
        last_attempt_date      = date.today(),
        customer_name          = notes.get("customer_name") or "Customer",
        customer_language_pref = notes.get("customer_language_pref", "hinglish"),
        payment_method         = None,
        past_recovery_success  = False,
        checkout_session_id    = entity.get("id"),
        hours_since_checkout   = hours,
    )


def map_checkout_event(payload: dict) -> TransactionFailure:
    """
    Dispatcher: routes to the right mapper based on event type.
    Called from the API's /webhooks/razorpay handler.

    Supported events:
      payment_link.expired  → map_payment_link_expired()
      order.abandoned       → map_order_abandoned()

    Raises ValueError for unknown or unsupported event types.
    """
    event = payload.get("event", "")
    if event == "payment_link.expired":
        return map_payment_link_expired(payload)
    elif event in ("order.abandoned", "order.paid"):
        return map_order_abandoned(payload)
    else:
        raise ValueError(f"checkout_mapper: unsupported event type '{event}'")

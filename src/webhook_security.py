"""
webhook_security.py — Razorpay webhook signature verification + idempotency.

Razorpay signs webhooks with HMAC-SHA256 of "{event_id}|{timestamp}" using the
webhook secret. Real Razorpay deliveries do NOT send X-Api-Key — so webhook
auth is signature-based, separate from the REST API key.

Env:
  RAZORPAY_WEBHOOK_SECRET  — required in production; if unset, signature check
                             is skipped only when MOCK_MODE=true / ALLOW_INSECURE_WEBHOOKS=true
"""

from __future__ import annotations
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from db.models import ProcessedWebhook

WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
ALLOW_INSECURE = (
    os.environ.get("ALLOW_INSECURE_WEBHOOKS", "").lower() == "true"
    or os.environ.get("MOCK_MODE", "true").lower() != "false"
)


def verify_razorpay_signature(
    body: bytes,
    signature: Optional[str],
) -> bool:
    """
    Verify X-Razorpay-Signature header.

    Razorpay docs: signature = HMAC-SHA256(webhook_secret, raw_body).
    Returns True if valid (or insecure mode allowed for local demo).
    """
    if not WEBHOOK_SECRET:
        if ALLOW_INSECURE:
            return True
        return False

    if not signature:
        return False

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def check_and_mark_event(
    session: Session,
    event_id: Optional[str],
    event_type: str,
    outcome: str = "processed",
) -> bool:
    """
    Idempotency gate. Returns True if this is a NEW event (caller should process).
    Returns False if already seen (caller should skip / return duplicate).

    If event_id is missing, always allows processing (non-idempotent path).
    """
    if not event_id:
        return True

    existing = session.get(ProcessedWebhook, event_id)
    if existing is not None:
        return False

    session.add(ProcessedWebhook(
        event_id=event_id,
        event_type=event_type,
        processed_at=datetime.now(timezone.utc),
        outcome=outcome,
    ))
    return True


def mark_event_outcome(
    session: Session,
    event_id: Optional[str],
    outcome: str,
) -> None:
    if not event_id:
        return
    row = session.get(ProcessedWebhook, event_id)
    if row:
        row.outcome = outcome

"""
db/compliance.py — DB-level compliance guards (goal 7).

These enforce the attempt cap (max 3) and incentive cap (1 discount per
transaction) by querying the PERSISTENT audit_log — not an in-memory set.

Why this matters vs. executor_v2.py's in-memory check:
  - executor_v2.py resets on every run. If a record was processed in a prior
    run, the in-memory set has no memory of it.
  - These guards check the DB, so they hold across runs, restarts, and
    concurrent API calls.

Called by the API layer BEFORE routing — if a cap is already met, the action
is forced to ESCALATE_TO_HUMAN, never bypassed.

Compliance rule (goal 7, hard constraint):
  - Hard-fail reasons (mandate_revoked, hard_decline) are handled by the
    router itself, not here. The router's escalation for those is structural
    and cannot be overridden by any API call.
  - These guards cover the attempt/discount caps only.
"""

from __future__ import annotations
from sqlalchemy.orm import Session
from .models import AuditLog

MAX_ATTEMPTS = 3      # must match router_v2.MAX_ATTEMPTS
MAX_INCENTIVES = 1    # one discount per transaction, hard


class ComplianceViolation(Exception):
    """Raised when a DB-level compliance guard trips. Caller converts to escalation."""
    pass


def check_attempt_cap(session: Session, record_id: str) -> None:
    """
    Raise ComplianceViolation if this record already has MAX_ATTEMPTS audit entries.
    Call this BEFORE routing so the action never reaches the payment API.
    """
    count = (
        session.query(AuditLog)
        .filter(AuditLog.record_id == record_id)
        .count()
    )
    if count >= MAX_ATTEMPTS:
        raise ComplianceViolation(
            f"record_id={record_id} already has {count} audit entries "
            f"(cap={MAX_ATTEMPTS}). Forcing escalation."
        )


def check_incentive_cap(session: Session, record_id: str) -> None:
    """
    Raise ComplianceViolation if this record already received a SEND_INCENTIVE_LINK.
    Call this AFTER routing IF the router chose SEND_INCENTIVE_LINK.
    """
    count = (
        session.query(AuditLog)
        .filter(
            AuditLog.record_id == record_id,
            AuditLog.action_taken == "SEND_INCENTIVE_LINK",
        )
        .count()
    )
    if count >= MAX_INCENTIVES:
        raise ComplianceViolation(
            f"record_id={record_id} already received {count} incentive discount(s) "
            f"(cap={MAX_INCENTIVES}). Forcing escalation instead."
        )


def get_attempt_count_from_db(session: Session, record_id: str) -> int:
    """Returns the number of audit entries for this record (for logging/reporting)."""
    return (
        session.query(AuditLog)
        .filter(AuditLog.record_id == record_id)
        .count()
    )

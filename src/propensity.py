"""
propensity.py — Lightweight propensity-to-pay scoring (Diagnose-layer AI).

Pure heuristic / logistic-style score — no external ML deps for buildathon.
Gates incentives and early escalation:
  score >= 0.55  → high propensity — prefer retry / payment link, skip discount
  score 0.25–0.55 → medium — allow incentive if router chose it
  score < 0.25   → low — escalate early, don't burn discount budget

Routing stays deterministic; this module only advises / overrides discount waste.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schemas import TransactionFailure

# Soft declines that historically recover well with delayed retry
_SOFT_REASONS = {
    "insufficient_funds",
    "bank_decline",
    "bank_downtime",
}
_HARD_REASONS = {
    "hard_decline",
    "mandate_revoked",
    "promise_broken",
}


def score_propensity(record: "TransactionFailure") -> float:
    """
    Returns propensity-to-pay in [0.0, 1.0].
    Higher = more likely to recover without a discount.
    """
    score = 0.45  # base prior

    reason = record.failure_reason.value if hasattr(record.failure_reason, "value") else str(record.failure_reason)

    if reason in _SOFT_REASONS:
        score += 0.20
    elif reason in _HARD_REASONS:
        score -= 0.30
    elif reason in ("card_expired", "3ds_auth_fail"):
        score += 0.05  # recoverable with customer action
    elif reason == "cart_abandoned":
        hours = record.hours_since_checkout or 24
        if hours < 1:
            score += 0.25
        elif hours <= 24:
            score += 0.10
        else:
            score -= 0.20

    if record.past_recovery_success:
        score += 0.18

    # Attempt fatigue
    attempts = record.attempt_count or 0
    score -= 0.12 * attempts

    # Amount: mid-ticket recovers better than very high (friction) or tiny (ignore)
    amt = record.amount_inr or 0
    if 500 <= amt <= 15000:
        score += 0.08
    elif amt > 50000:
        score -= 0.10

    # Subscriptions with past success are sticky
    txn_type = record.transaction_type.value if hasattr(record.transaction_type, "value") else str(record.transaction_type)
    if txn_type == "subscription" and record.past_recovery_success:
        score += 0.07

    # Clamp
    return round(max(0.0, min(1.0, score)), 3)


def should_allow_incentive(score: float) -> bool:
    """Only spend discount budget when propensity is medium-low (worth nudging)."""
    return 0.20 <= score < 0.55


def should_escalate_early(score: float) -> bool:
    """Very low propensity → don't burn attempts; escalate."""
    return score < 0.20

"""
Router v4 — Track 03 Alignment.

All 7 Buildathon directions + closed-loop helpers:
  1. Payment degradation  — BANK_DOWNTIME (scheduler defers retry)
  2. Checkout drop-off    — urgency-tiered cart recovery
  3. Failed-subscription  — dedicated grace / dunning branch
  4. B2B receivables      — 5-tier aging chain (+ voice before legal)
  5. Mandate sequencer    — setup link vs escalate
  6. Hinglish voice       — INITIATE_VOICE_CALL for high-value / broken promise
  7. Promise-to-pay       — PROMISE_BROKEN → voice or high-priority escalate

Diagnose layer: propensity score gates incentives & early escalation.
All decisions remain deterministic and Pydantic-validated.
"""

from datetime import date
from schemas import (
    TransactionFailure, TransactionType, FailureReason,
    AgentAction, ActionType,
)
from propensity import score_propensity, should_allow_incentive, should_escalate_early

MAX_ATTEMPTS = 3
MAX_DISCOUNT_PCT = 20
SUBSCRIPTION_GRACE_DAYS = 7
VOICE_AMOUNT_THRESHOLD_INR = 10_000


def _lang(record: TransactionFailure) -> str:
    return record.customer_language_pref or "hinglish"


def _with_score(action: AgentAction, score: float) -> AgentAction:
    return action.model_copy(update={"propensity_score": score})


def _maybe_defer_retry(action: AgentAction) -> AgentAction:
    """Mark RETRY_PAYMENT with delay for scheduler instead of immediate execute."""
    if (
        action.action == ActionType.RETRY_PAYMENT
        and action.retry_delay_hours
        and action.retry_delay_hours > 0
    ):
        return action.model_copy(update={"defer_execution": True})
    return action


def route(record: TransactionFailure) -> AgentAction:
    rid = record.record_id
    reason = record.failure_reason
    attempts = record.attempt_count
    lang = _lang(record)
    score = score_propensity(record)

    # ================================================================
    # CHECKOUT ABANDONMENT
    # ================================================================
    if record.transaction_type == TransactionType.CHECKOUT_ABANDONMENT:
        hours = record.hours_since_checkout or 0
        if hours < 1:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.SEND_CHECKOUT_RECOVERY,
                reasoning=(
                    f"Cart abandoned {hours}h ago — high-intent window (<1h). "
                    "Send recovery link immediately before the session goes cold."
                ),
                stop_after_this=False,
                message_language=lang,
            ), score)
        elif hours <= 24:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.SEND_CHECKOUT_RECOVERY,
                reasoning=(
                    f"Cart abandoned {hours}h ago — still recoverable. "
                    "Send reminder with urgency nudge."
                ),
                stop_after_this=False,
                message_language=lang,
            ), score)
        else:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.ESCALATE_TO_HUMAN,
                reasoning=(
                    f"Cart abandoned {hours}h ago — cold lead. "
                    "Automated link unlikely to convert; needs personal outreach."
                ),
                stop_after_this=True,
                escalation_priority="low",
                message_language=lang,
            ), score)

    # ================================================================
    # PROMISE BROKEN — voice for high value, else human escalate
    # ================================================================
    if reason == FailureReason.PROMISE_BROKEN:
        if record.amount_inr >= VOICE_AMOUNT_THRESHOLD_INR or attempts >= 1:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.INITIATE_VOICE_CALL,
                reasoning=(
                    "Customer broke a promise-to-pay. High-priority Hinglish voice "
                    "outreach before further human escalation."
                ),
                stop_after_this=False,
                message_language=lang if lang in ("hi", "hinglish") else "hinglish",
            ), score)
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning=(
                "Customer broke a promise-to-pay commitment. "
                "Escalating with high priority — second chance requires human negotiation."
            ),
            stop_after_this=True,
            escalation_priority="high",
            message_language=lang,
        ), score)

    # ================================================================
    # GATE 1: attempt cap
    # ================================================================
    if attempts >= MAX_ATTEMPTS:
        # High-value: one voice attempt before final human escalate
        if record.amount_inr >= VOICE_AMOUNT_THRESHOLD_INR and attempts == MAX_ATTEMPTS:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.INITIATE_VOICE_CALL,
                reasoning=(
                    f"Attempt cap ({MAX_ATTEMPTS}) reached on high-value txn "
                    f"(₹{record.amount_inr}). Final Hinglish voice recovery before human queue."
                ),
                stop_after_this=True,
                message_language="hinglish",
            ), score)
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning=f"Attempt count ({attempts}) hit max ({MAX_ATTEMPTS}). Escalating.",
            stop_after_this=True,
            escalation_priority="medium",
            message_language=lang,
        ), score)

    # ================================================================
    # Propensity early escalate (Diagnose AI gate)
    # ================================================================
    if should_escalate_early(score) and attempts >= 1:
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning=(
                f"Propensity-to-pay score {score:.2f} is below threshold — "
                "early escalate to avoid wasting attempt budget."
            ),
            stop_after_this=True,
            escalation_priority="medium",
            message_language=lang,
        ), score)

    # ================================================================
    # GATE 2: mandate revoked — SMART SEQUENCER
    # ================================================================
    if reason == FailureReason.MANDATE_REVOKED:
        if attempts == 0 and record.past_recovery_success:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.SEND_MANDATE_SETUP,
                reasoning=(
                    "First mandate revocation with prior recovery success — "
                    "likely a bank glitch, not intentional cancellation. "
                    "Send fresh NACH/emandate setup link before escalating."
                ),
                stop_after_this=False,
                message_language=lang,
            ), score)
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning="Mandate revoked requires fresh customer consent — cannot automate.",
            stop_after_this=True,
            escalation_priority="high",
            message_language=lang,
        ), score)

    # ================================================================
    # GATE 3: hard decline
    # ================================================================
    if reason == FailureReason.HARD_DECLINE:
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning="Hard decline from issuer — retrying risks further blocks. Escalate.",
            stop_after_this=True,
            escalation_priority="high",
            message_language=lang,
        ), score)

    # ================================================================
    # FAILED-SUBSCRIPTION RECOVERY (dedicated Track 03 branch)
    # ================================================================
    if record.transaction_type == TransactionType.SUBSCRIPTION:
        return _with_score(_route_subscription(record, score, lang), score)

    # ================================================================
    # B2B INVOICE — 5-TIER RECEIVABLES CHASER + voice before legal
    # ================================================================
    if record.transaction_type == TransactionType.B2B_INVOICE:
        return _with_score(_route_b2b(record, score, lang), score)

    # ================================================================
    # BANK DOWNTIME — silent deferred retry
    # ================================================================
    if reason == FailureReason.BANK_DOWNTIME:
        return _with_score(_maybe_defer_retry(AgentAction(
            record_id=rid,
            action=ActionType.RETRY_PAYMENT,
            reasoning="Bank downtime is infra-side, not customer fault — silent retry.",
            stop_after_this=False,
            retry_delay_hours=4,
            message_language=lang,
        )), score)

    # ================================================================
    # CARD EXPIRED / 3DS
    # ================================================================
    if reason in (FailureReason.CARD_EXPIRED, FailureReason.THREE_DS_AUTH_FAIL):
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=f"{reason.value} needs customer to update/re-authenticate payment method.",
            stop_after_this=(attempts + 1 >= MAX_ATTEMPTS),
            message_language=lang,
        ), score)

    # ================================================================
    # INSUFFICIENT FUNDS
    # ================================================================
    if reason == FailureReason.INSUFFICIENT_FUNDS:
        if attempts == 0:
            return _with_score(_maybe_defer_retry(AgentAction(
                record_id=rid,
                action=ActionType.RETRY_PAYMENT,
                reasoning="First funds failure — retry after a delay near typical salary cycle.",
                stop_after_this=False,
                retry_delay_hours=72,
                message_language=lang,
            )), score)
        elif attempts == 1 and record.past_recovery_success:
            return _with_score(_maybe_defer_retry(AgentAction(
                record_id=rid,
                action=ActionType.RETRY_PAYMENT,
                reasoning="Customer recovered before under similar conditions — retry again.",
                stop_after_this=False,
                retry_delay_hours=48,
                message_language=lang,
            )), score)
        else:
            return _with_score(_maybe_incentive_or_link(record, score, lang, default_pct=10), score)

    # ================================================================
    # BANK DECLINE (soft)
    # ================================================================
    if reason == FailureReason.BANK_DECLINE:
        if attempts == 0:
            return _with_score(_maybe_defer_retry(AgentAction(
                record_id=rid,
                action=ActionType.RETRY_PAYMENT,
                reasoning="Bank declines are often transient — silent retry first.",
                stop_after_this=False,
                retry_delay_hours=6,
                message_language=lang,
            )), score)
        # High value + 2nd attempt → voice
        if attempts >= 1 and record.amount_inr >= VOICE_AMOUNT_THRESHOLD_INR:
            return _with_score(AgentAction(
                record_id=rid,
                action=ActionType.INITIATE_VOICE_CALL,
                reasoning=(
                    "Repeated bank decline on high-value payment — "
                    "Hinglish voice recovery before link fatigue."
                ),
                stop_after_this=False,
                message_language="hinglish",
            ), score)
        return _with_score(AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning="Repeated bank decline — ask customer to verify/switch payment method.",
            stop_after_this=(attempts + 1 >= MAX_ATTEMPTS),
            message_language=lang,
        ), score)

    # ================================================================
    # FALLBACK
    # ================================================================
    return _with_score(AgentAction(
        record_id=rid,
        action=ActionType.ESCALATE_TO_HUMAN,
        reasoning=f"Unhandled failure_reason={reason.value} — routed to human rather than guessing.",
        stop_after_this=True,
        escalation_priority="medium",
        message_language=lang,
    ), score)


def _maybe_incentive_or_link(
    record: TransactionFailure,
    score: float,
    lang: str,
    default_pct: int = 10,
) -> AgentAction:
    """Propensity gate: only send incentive when score says discount helps."""
    if should_allow_incentive(score):
        return AgentAction(
            record_id=record.record_id,
            action=ActionType.SEND_INCENTIVE_LINK,
            reasoning=(
                f"Propensity {score:.2f} in discount window — "
                f"one-time {default_pct}% retention offer before escalation."
            ),
            stop_after_this=False,
            incentive_discount_pct=min(default_pct, MAX_DISCOUNT_PCT),
            message_language=lang,
        )
    # High propensity — don't discount; send plain link
    if score >= 0.55:
        return AgentAction(
            record_id=record.record_id,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=(
                f"Propensity {score:.2f} high — skip discount to protect margin; "
                "send plain payment link."
            ),
            stop_after_this=False,
            payment_link_amount_inr=record.amount_inr,
            message_language=lang,
        )
    return AgentAction(
        record_id=record.record_id,
        action=ActionType.ESCALATE_TO_HUMAN,
        reasoning=(
            f"Propensity {score:.2f} too low for automated discount — escalate."
        ),
        stop_after_this=True,
        escalation_priority="medium",
        message_language=lang,
    )


def _route_subscription(
    record: TransactionFailure,
    score: float,
    lang: str,
) -> AgentAction:
    """
    Dedicated failed-subscription recovery:
      days_past_due / attempt_count drive grace → retry → link → incentive → escalate.
    """
    rid = record.record_id
    reason = record.failure_reason
    attempts = record.attempt_count
    days = record.days_past_due
    if days is None and record.last_attempt_date:
        days = (date.today() - record.last_attempt_date).days
    days = days or 0

    # Past grace period → involuntary churn path
    if days > SUBSCRIPTION_GRACE_DAYS and attempts >= 2:
        if record.amount_inr >= VOICE_AMOUNT_THRESHOLD_INR:
            return AgentAction(
                record_id=rid,
                action=ActionType.INITIATE_VOICE_CALL,
                reasoning=(
                    f"Subscription {record.subscription_id} past {SUBSCRIPTION_GRACE_DAYS}d "
                    f"grace ({days}d past due). Hinglish voice retention call."
                ),
                stop_after_this=False,
                message_language="hinglish",
            )
        return AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning=(
                f"Subscription {record.subscription_id} past grace ({days}d) — "
                "involuntary churn risk; retention agent required "
                "(cancel-at-period-end vs retry decision)."
            ),
            stop_after_this=True,
            escalation_priority="high",
            message_language=lang,
        )

    # Card / 3DS — customer must update method
    if reason in (FailureReason.CARD_EXPIRED, FailureReason.THREE_DS_AUTH_FAIL):
        return AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=(
                f"Subscription {record.subscription_id} {reason.value} — "
                "send link so customer can update payment method."
            ),
            stop_after_this=(attempts + 1 >= MAX_ATTEMPTS),
            payment_link_amount_inr=record.amount_inr,
            message_language=lang,
        )

    # Soft decline first failure → deferred salary-cycle retry
    if attempts == 0 and reason in (
        FailureReason.INSUFFICIENT_FUNDS,
        FailureReason.BANK_DECLINE,
        FailureReason.BANK_DOWNTIME,
    ):
        delay = 72 if reason == FailureReason.INSUFFICIENT_FUNDS else 48
        return _maybe_defer_retry(AgentAction(
            record_id=rid,
            action=ActionType.RETRY_PAYMENT,
            reasoning=(
                f"Subscription {record.subscription_id} first soft failure "
                f"({reason.value}) within grace — deferred retry in {delay}h."
            ),
            stop_after_this=False,
            retry_delay_hours=delay,
            message_language=lang,
        ))

    # 2nd failure / liquidity → payment link (UPI-friendly messaging)
    if attempts == 1:
        return AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=(
                f"Subscription {record.subscription_id} 2nd failure — "
                "send payment link (customer can complete via UPI/card)."
            ),
            stop_after_this=False,
            payment_link_amount_inr=record.amount_inr,
            message_language=lang,
        )

    # 3rd failure → incentive or escalate via propensity
    return _maybe_incentive_or_link(record, score, lang, default_pct=10)


def _route_b2b(record: TransactionFailure, score: float, lang: str) -> AgentAction:
    days_overdue = 0
    if record.due_date:
        days_overdue = (date.today() - record.due_date).days
    counterparty = record.counterparty_business_name or "the counterparty"
    rid = record.record_id

    if days_overdue > 60:
        return AgentAction(
            record_id=rid,
            action=ActionType.ESCALATE_TO_HUMAN,
            reasoning=(
                f"Invoice {days_overdue}d overdue ({counterparty}) — "
                "past collections threshold. Legal/AR team required."
            ),
            stop_after_this=True,
            escalation_priority="critical",
            message_language=lang,
        )
    elif days_overdue > 30:
        # Voice before AR/legal handoff
        return AgentAction(
            record_id=rid,
            action=ActionType.INITIATE_VOICE_CALL,
            reasoning=(
                f"Invoice {days_overdue}d overdue ({counterparty}) — "
                "Hinglish/English voice chase before AR escalation."
            ),
            stop_after_this=False,
            message_language=lang if lang in ("en", "hi", "hinglish") else "en",
        )
    elif days_overdue > 14:
        if should_allow_incentive(score) or score >= 0.55:
            return AgentAction(
                record_id=rid,
                action=ActionType.SEND_INCENTIVE_LINK,
                reasoning=(
                    f"Invoice {days_overdue}d overdue ({counterparty}) — "
                    "offering 2% early-payment settlement discount."
                ),
                stop_after_this=False,
                incentive_discount_pct=2,
                message_language=lang,
            )
        return AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=(
                f"Invoice {days_overdue}d overdue ({counterparty}) — "
                "formal notice; propensity too low for discount."
            ),
            stop_after_this=False,
            payment_link_amount_inr=record.amount_inr,
            message_language=lang,
        )
    elif days_overdue > 7:
        return AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=(
                f"Invoice {days_overdue}d overdue ({counterparty}) — "
                "formal payment notice with due-date reminder."
            ),
            stop_after_this=False,
            payment_link_amount_inr=record.amount_inr,
            message_language=lang,
        )
    else:
        return AgentAction(
            record_id=rid,
            action=ActionType.SEND_PAYMENT_LINK,
            reasoning=(
                f"Invoice {days_overdue}d overdue ({counterparty}) — "
                "polite first reminder."
            ),
            stop_after_this=False,
            payment_link_amount_inr=record.amount_inr,
            message_language=lang,
        )

"""
Schema layer for Revenue Recovery Agent — v4 (Track 03 Alignment).

Additions over v3 (all backward-compatible, additive only):
  ActionType         : + INITIATE_VOICE_CALL
  TransactionFailure : + days_past_due (subscription grace), recovery_status
  AgentAction        : + propensity_score, schedule_at_hours (deferred execution hint)

Existing data is fully compatible — all new fields are Optional with sensible defaults.
"""

from __future__ import annotations
from datetime import date
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# INPUT: unified transaction failure record
# ---------------------------------------------------------------------------

class TransactionType(str, Enum):
    SUBSCRIPTION        = "subscription"
    B2B_INVOICE         = "b2b_invoice"
    ONE_TIME_PAYMENT    = "one_time_payment"
    CHECKOUT_ABANDONMENT = "checkout_abandonment"


class FailureReason(str, Enum):
    INSUFFICIENT_FUNDS  = "insufficient_funds"
    CARD_EXPIRED        = "card_expired"
    BANK_DECLINE        = "bank_decline"
    THREE_DS_AUTH_FAIL  = "3ds_auth_fail"
    MANDATE_REVOKED     = "mandate_revoked"
    BANK_DOWNTIME       = "bank_downtime"
    INVOICE_OVERDUE     = "invoice_overdue"
    HARD_DECLINE        = "hard_decline"
    CART_ABANDONED      = "cart_abandoned"
    PROMISE_BROKEN      = "promise_broken"


class TransactionFailure(BaseModel):
    """Unified record. Type-specific fields are Optional and only populated
    when relevant — validated by the model_validator below."""

    record_id: str
    transaction_type: TransactionType
    failure_reason: FailureReason
    amount_inr: int = Field(gt=0)
    attempt_count: int = Field(ge=0, default=0)
    last_attempt_date: date
    customer_name: str
    customer_language_pref: str = "hinglish"   # "hinglish" | "hi" | "en" — drives message templates
    payment_method: Optional[str] = None
    past_recovery_success: bool = False

    # subscription-specific
    subscription_id: Optional[str] = None
    days_past_due: Optional[int] = Field(default=None, ge=0)  # days since first failed charge

    # B2B invoice-specific
    invoice_id: Optional[str] = None
    due_date: Optional[date] = None
    po_reference: Optional[str] = None
    counterparty_business_name: Optional[str] = None

    # checkout abandonment-specific
    checkout_session_id: Optional[str] = None
    hours_since_checkout: Optional[int] = Field(default=None, ge=0)

    # promise-to-pay specific
    promised_payment_date: Optional[date] = None

    # closed-loop recovery (set by payment.captured webhook, not by router)
    recovery_status: Optional[str] = None  # "pending" | "recovered" | None

    @model_validator(mode="after")
    def check_type_specific_fields(self) -> "TransactionFailure":
        if self.transaction_type == TransactionType.SUBSCRIPTION:
            if not self.subscription_id:
                raise ValueError("subscription_id required when transaction_type=subscription")
        if self.transaction_type == TransactionType.B2B_INVOICE:
            if not self.invoice_id or not self.due_date:
                raise ValueError("invoice_id and due_date required when transaction_type=b2b_invoice")
        return self

    @field_validator("amount_inr")
    @classmethod
    def sane_amount_ceiling(cls, v: int) -> int:
        if v > 10_000_000:
            raise ValueError(f"amount_inr={v} exceeds sanity ceiling — reject record")
        return v


# ---------------------------------------------------------------------------
# OUTPUT: strict agent action schema (the guardrail layer)
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    RETRY_PAYMENT           = "RETRY_PAYMENT"
    SEND_INCENTIVE_LINK     = "SEND_INCENTIVE_LINK"
    SEND_PAYMENT_LINK       = "SEND_PAYMENT_LINK"
    ESCALATE_TO_HUMAN       = "ESCALATE_TO_HUMAN"
    NO_ACTION               = "NO_ACTION"
    SEND_CHECKOUT_RECOVERY  = "SEND_CHECKOUT_RECOVERY"
    SEND_MANDATE_SETUP      = "SEND_MANDATE_SETUP"
    LOG_PROMISE_TO_PAY      = "LOG_PROMISE_TO_PAY"
    INITIATE_VOICE_CALL     = "INITIATE_VOICE_CALL"  # Hinglish voice recovery (Track 03)


class AgentAction(BaseModel):
    """
    Every routing decision MUST produce one of these, validated before execution.
    """

    record_id: str
    action: ActionType
    reasoning: str = Field(min_length=10, max_length=500)
    stop_after_this: bool

    # execution parameters
    retry_delay_hours: Optional[int]    = Field(default=None, ge=0, le=168)
    incentive_discount_pct: Optional[int] = Field(default=None, ge=0, le=20)
    payment_link_amount_inr: Optional[int] = Field(default=None, gt=0)
    escalation_priority: Optional[str]  = None

    message_language: str               = "en"          # "en" | "hi" | "hinglish"
    follow_up_date: Optional[date]      = None          # for LOG_PROMISE_TO_PAY
    propensity_score: Optional[float]   = Field(default=None, ge=0.0, le=1.0)
    # When set, executor schedules instead of executing immediately
    defer_execution: bool               = False

    @model_validator(mode="after")
    def check_action_params(self) -> "AgentAction":
        if self.action == ActionType.RETRY_PAYMENT and self.retry_delay_hours is None:
            raise ValueError("RETRY_PAYMENT requires retry_delay_hours")
        if self.action == ActionType.SEND_INCENTIVE_LINK and self.incentive_discount_pct is None:
            raise ValueError("SEND_INCENTIVE_LINK requires incentive_discount_pct")
        if self.action == ActionType.SEND_INCENTIVE_LINK and self.incentive_discount_pct > 20:
            raise ValueError("incentive_discount_pct exceeds hard cap of 20%")
        if self.action == ActionType.ESCALATE_TO_HUMAN and self.escalation_priority is None:
            raise ValueError("ESCALATE_TO_HUMAN requires escalation_priority")
        return self


# ---------------------------------------------------------------------------
# quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from datetime import date

    # existing: valid subscription
    rec = TransactionFailure(
        record_id="TXN0001", transaction_type=TransactionType.SUBSCRIPTION,
        failure_reason=FailureReason.INSUFFICIENT_FUNDS, amount_inr=999,
        attempt_count=0, last_attempt_date=date.today(),
        customer_name="Test User", subscription_id="SUB0001",
    )
    print("Subscription record OK:", rec.record_id)

    # new: checkout abandonment
    cart = TransactionFailure(
        record_id="CART001", transaction_type=TransactionType.CHECKOUT_ABANDONMENT,
        failure_reason=FailureReason.CART_ABANDONED, amount_inr=2499,
        attempt_count=0, last_attempt_date=date.today(),
        customer_name="Rahul Sharma", hours_since_checkout=2,
        checkout_session_id="order_xyz123",
    )
    print("Checkout abandonment record OK:", cart.record_id, f"({cart.hours_since_checkout}h ago)")

    # new: mandate setup action
    action = AgentAction(
        record_id="TXN0001", action=ActionType.SEND_MANDATE_SETUP,
        reasoning="First mandate revocation with prior recovery success — retry mandate setup.",
        stop_after_this=False, message_language="hinglish",
    )
    print("Mandate setup action OK:", action.action, f"lang={action.message_language}")
    print("All schema checks passed.")

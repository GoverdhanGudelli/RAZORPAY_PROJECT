"""
db/models.py — SQLAlchemy ORM models.

Two tables (permanent, goal-traced):
  transactions  — mirrors TransactionFailure schema exactly (goal 1: detect revenue at risk)
  audit_log     — append-only decision+execution log (goal 5: full audit trail)

The recovery_metrics VIEW is created separately in engine.py's init_db() using
raw DDL — it's a computed projection, not stored data, so it lives in the view layer.

Design rules enforced here:
  - audit_log has no cascade delete — once written, entries survive forever.
  - No user/tenant/multi-merchant columns — out of scope per the build prompt.
  - execution is stored as JSON (auto-serialized by SQLAlchemy) so the full
    Razorpay API response is preserved verbatim, including test_mode_amount_capped
    and run_suffix fields added by razorpay_client.py.
"""

from __future__ import annotations
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey,
    Integer, String, Text, JSON,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    """
    Mirrors every field of schemas.TransactionFailure.
    record_id is the natural PK — it comes from the upstream system/CSV and is
    guaranteed unique per failed transaction. Upserted by the API on each call.
    """
    __tablename__ = "transactions"

    record_id                = Column(String,  primary_key=True)
    transaction_type         = Column(String,  nullable=False)          # enum value as str
    failure_reason           = Column(String,  nullable=False)          # enum value as str
    amount_inr               = Column(Integer, nullable=False)
    attempt_count            = Column(Integer, nullable=False, default=0)
    last_attempt_date        = Column(Date,    nullable=False)
    customer_name            = Column(String,  nullable=False)
    customer_language_pref   = Column(String,  nullable=False, default="en")
    payment_method           = Column(String,  nullable=True)
    past_recovery_success    = Column(Boolean, nullable=False, default=False)

    # subscription-specific
    subscription_id          = Column(String,  nullable=True)
    days_past_due            = Column(Integer, nullable=True)

    # B2B invoice-specific
    invoice_id               = Column(String,  nullable=True)
    due_date                 = Column(Date,    nullable=True)
    po_reference             = Column(String,  nullable=True)
    counterparty_business_name = Column(String, nullable=True)

    # checkout / promise
    checkout_session_id      = Column(String,  nullable=True)
    hours_since_checkout     = Column(Integer, nullable=True)
    promised_payment_date    = Column(Date,    nullable=True)

    # closed-loop recovery attribution
    recovery_status          = Column(String,  nullable=True, default="pending")
    recovered_amount_inr     = Column(Integer, nullable=True)
    recovered_at             = Column(DateTime(timezone=True), nullable=True)

    # relationship — audit entries for this transaction
    audit_entries = relationship("AuditLog", back_populates="transaction",
                                 cascade="save-update, merge")  # NO delete cascade — goal 5


class AuditLog(Base):
    """
    Append-only audit trail. Every routing decision + execution result lands here.
    Mirrors the JSONL entry shape from executor_v2.py so migration is lossless.

    APPEND-ONLY invariant: the API layer never issues UPDATE or DELETE against
    this table. There is no route that modifies past entries — compliance goal 7.
    The 'execution' JSON blob stores the full razorpay_client response, including
    api_response (which may be null for escalations or rate-limited calls).
    """
    __tablename__ = "audit_log"

    id              = Column(Integer,  primary_key=True, autoincrement=True)
    timestamp       = Column(DateTime(timezone=True), nullable=False)
    record_id       = Column(String, ForeignKey("transactions.record_id",
                                                 ondelete="RESTRICT"), nullable=False)
    transaction_type  = Column(String,  nullable=False)
    failure_reason    = Column(String,  nullable=False)
    amount_inr        = Column(Integer, nullable=False)
    attempt_count     = Column(Integer, nullable=False)
    action_taken      = Column(String,  nullable=False)   # ActionType enum value
    reasoning         = Column(Text,    nullable=False)
    stop_after_this   = Column(Boolean, nullable=False)
    execution         = Column(JSON,    nullable=True)     # full exec result dict

    transaction = relationship("Transaction", back_populates="audit_entries")


class PromiseToPay(Base):
    """
    Tracks customer verbal/written pay commitments (promise-to-pay tracker).
    Written via POST /promises by a collections agent after a customer call.
    Checked daily via GET /promises/overdue to surface broken promises.

    status values:
      pending   — promise made, due date not yet passed
      fulfilled — customer paid (updated via PATCH /promises/{id})
      broken    — due date passed, still pending (set automatically by /promises/overdue)
      partial   — customer paid less than the promised amount
    """
    __tablename__ = "promises"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    record_id           = Column(String, ForeignKey("transactions.record_id",
                                                    ondelete="RESTRICT"), nullable=False)
    promised_date       = Column(Date,    nullable=False)       # date customer committed to pay by
    promised_amount_inr = Column(Integer, nullable=False)
    contact_name        = Column(String,  nullable=True)        # who made the promise
    contact_phone       = Column(String,  nullable=True)
    status              = Column(String,  nullable=False, default="pending")
    notes               = Column(Text,    nullable=True)        # free-text from the agent call
    created_at          = Column(DateTime(timezone=True), nullable=False)
    updated_at          = Column(DateTime(timezone=True), nullable=True)

    transaction = relationship("Transaction")


class RecoveryEvent(Base):
    """
    Closed-loop recovery attribution (Track 03 The Bar: measured money recovered).
    Written when payment.captured or payment_link.paid webhooks arrive.
    Links Razorpay payment IDs back to our record_id via notes/reference_id.
    """
    __tablename__ = "recovery_events"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    record_id         = Column(String, ForeignKey("transactions.record_id",
                                                  ondelete="RESTRICT"), nullable=False)
    razorpay_payment_id = Column(String, nullable=True)
    razorpay_event_id = Column(String, nullable=True, unique=True)  # idempotency
    amount_inr        = Column(Integer, nullable=False)
    event_type        = Column(String,  nullable=False)  # payment.captured | payment_link.paid
    recovered_at      = Column(DateTime(timezone=True), nullable=False)
    raw_payload       = Column(JSON,    nullable=True)

    transaction = relationship("Transaction")


class PendingAction(Base):
    """
    Scheduled recovery actions — enforces retry_delay_hours and B2B dunning cadence.
    Created when router returns RETRY_PAYMENT with retry_delay_hours > 0 (defer).
    Processed by POST /scheduler/run or a cron worker.
    """
    __tablename__ = "pending_actions"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    record_id        = Column(String, ForeignKey("transactions.record_id",
                                                 ondelete="RESTRICT"), nullable=False)
    action_type      = Column(String,  nullable=False)
    action_payload   = Column(JSON,    nullable=False)   # serialized AgentAction fields
    record_payload   = Column(JSON,    nullable=False)   # serialized TransactionFailure
    execute_at       = Column(DateTime(timezone=True), nullable=False)
    status           = Column(String,  nullable=False, default="pending")
    # pending | executed | cancelled | failed
    created_at       = Column(DateTime(timezone=True), nullable=False)
    executed_at      = Column(DateTime(timezone=True), nullable=True)
    result           = Column(JSON,    nullable=True)

    transaction = relationship("Transaction")


class ProcessedWebhook(Base):
    """
    Idempotency store for Razorpay webhook event IDs.
    Prevents duplicate processing when Razorpay retries delivery.
    """
    __tablename__ = "processed_webhooks"

    event_id     = Column(String, primary_key=True)   # Razorpay event id
    event_type   = Column(String, nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=False)
    outcome      = Column(String, nullable=True)      # processed | ignored | recovered


class VoiceCall(Base):
    """
    Persists every INITIATE_VOICE_CALL execution (real Vapi calls or mock stubs).

    status values:
      initiated  — Vapi call created, ringing / in-progress
      connected  — customer answered
      completed  — call ended normally
      no_answer  — customer did not pick up
      failed     — Vapi API error
      mock       — mock mode, no real call placed

    outcome values:
      link_sent         — agent called send_payment_link during the call
      ptp_scheduled     — customer promised to pay (schedule_ptp_retry)
      opted_out         — customer asked to stop (mark_opt_out)
      escalated         — escalate_to_human triggered
      no_action         — call completed with no tool called
      abandoned         — call dropped / no_answer
    """
    __tablename__ = "voice_calls"

    id               = Column(Integer,  primary_key=True, autoincrement=True)
    call_id          = Column(String,   nullable=False, unique=True)  # Vapi call ID (or mock ID)
    record_id        = Column(String,   ForeignKey("transactions.record_id",
                                                    ondelete="RESTRICT"), nullable=False)
    status           = Column(String,   nullable=False, default="initiated")
    outcome          = Column(String,   nullable=True)
    language         = Column(String,   nullable=False, default="hinglish")
    voice_script     = Column(Text,     nullable=True)      # opening script sent to Vapi
    transcript       = Column(JSON,     nullable=True)      # full Vapi transcript array
    payment_link_url = Column(String,   nullable=True)      # short_url if link was sent
    duration_sec     = Column(Integer,  nullable=True)
    mock             = Column(Boolean,  nullable=False, default=False)
    created_at       = Column(DateTime(timezone=True), nullable=False)
    completed_at     = Column(DateTime(timezone=True), nullable=True)
    raw_payload      = Column(JSON,     nullable=True)      # full Vapi webhook payload

    transaction = relationship("Transaction")

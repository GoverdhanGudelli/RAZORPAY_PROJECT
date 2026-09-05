"""
webhook_mapper.py — Maps a Razorpay payment.failed webhook payload → TransactionFailure.

Razorpay webhook shape (payment.failed event):
{
  "entity": "event",
  "event": "payment.failed",
  "contains": ["payment"],
  "payload": {
    "payment": {
      "entity": {
        "id": "pay_...",
        "amount": 50000,          # in paise
        "currency": "INR",
        "status": "failed",
        "method": "card" | "upi" | "netbanking" | ...,
        "error_code": "...",
        "error_description": "...",
        "error_source": "...",
        "error_reason": "...",
        "notes": {"record_id": "...", "customer_name": "...", ...},
        ...
      }
    }
  }
}

Mapping strategy:
  - record_id       : notes.record_id if set, else payment entity id ("pay_...")
  - amount_inr      : amount (paise) ÷ 100
  - failure_reason  : mapped from error_code + method (see ERROR_CODE_MAP)
  - transaction_type: from notes.transaction_type if present, else one_time_payment
  - customer_name   : notes.customer_name, else "Unknown"
  - attempt_count   : 1 (this is the first webhook-triggered attempt we know of)
  - payment_method  : method field from the payment entity
  - last_attempt_date: today (webhook is real-time)

Fields with no Razorpay equivalent that have required defaults:
  - customer_language_pref = "en"
  - past_recovery_success  = False

If the payload is structurally invalid (missing payment entity), raises ValueError
so the caller can return HTTP 400.
"""

from __future__ import annotations
from datetime import date
from schemas import (
    TransactionFailure, TransactionType, FailureReason,
)

# Maps Razorpay error_code → FailureReason.
# Razorpay error_codes: https://razorpay.com/docs/payments/payments/errors/
ERROR_CODE_MAP: dict[str, FailureReason] = {
    # Issuer responses
    "INSUFFICIENT_FUNDS":          FailureReason.INSUFFICIENT_FUNDS,
    "BAD_REQUEST_ERROR":           FailureReason.BANK_DECLINE,   # soft decline, default
    "PAYMENT_DECLINED":            FailureReason.BANK_DECLINE,
    "CARD_DECLINED":               FailureReason.BANK_DECLINE,
    "EXPIRED_CARD":                FailureReason.CARD_EXPIRED,
    "CARD_EXPIRED":                FailureReason.CARD_EXPIRED,
    "AUTHENTICATION_FAILED":       FailureReason.THREE_DS_AUTH_FAIL,
    "3DS_AUTH_FAIL":               FailureReason.THREE_DS_AUTH_FAIL,
    # Hard declines
    "LOST_CARD":                   FailureReason.HARD_DECLINE,
    "STOLEN_CARD":                 FailureReason.HARD_DECLINE,
    "DO_NOT_HONOUR":               FailureReason.HARD_DECLINE,
    "HARD_DECLINE":                FailureReason.HARD_DECLINE,
    "FRAUD_SUSPECTED":             FailureReason.HARD_DECLINE,
    # Infra / gateway
    "GATEWAY_ERROR":               FailureReason.BANK_DOWNTIME,
    "SERVER_ERROR":                FailureReason.BANK_DOWNTIME,
    "NETWORK_ERROR":               FailureReason.BANK_DOWNTIME,
    "TIMEOUT":                     FailureReason.BANK_DOWNTIME,
    # Mandate-related (subscription flows)
    "MANDATE_REVOKED":             FailureReason.MANDATE_REVOKED,
    "EMANDATE_REVOKED":            FailureReason.MANDATE_REVOKED,
    "NACH_DEBIT_REJECTED":         FailureReason.MANDATE_REVOKED,
}

TRANSACTION_TYPE_MAP: dict[str, TransactionType] = {
    "subscription":       TransactionType.SUBSCRIPTION,
    "b2b_invoice":        TransactionType.B2B_INVOICE,
    "one_time_payment":   TransactionType.ONE_TIME_PAYMENT,
}


def map_webhook_to_failure(payload: dict) -> TransactionFailure:
    """
    Converts a Razorpay payment.failed webhook payload into a TransactionFailure.

    Raises:
        ValueError — if the payload is missing required fields or the amount
                     is zero/negative after conversion.
    """
    try:
        entity = payload["payload"]["payment"]["entity"]
    except (KeyError, TypeError) as e:
        raise ValueError(
            f"Malformed webhook payload — expected payload.payment.entity: {e}"
        )

    # --- amount ---
    amount_paise = entity.get("amount", 0)
    amount_inr = int(amount_paise) // 100
    if amount_inr <= 0:
        raise ValueError(
            f"Webhook amount={amount_paise} paise converts to ₹{amount_inr} — invalid"
        )

    # --- notes (optional metadata we may have embedded at payment-link creation) ---
    notes: dict = entity.get("notes") or {}

    # --- record_id ---
    record_id = notes.get("record_id") or entity.get("id")
    if not record_id:
        raise ValueError("Cannot determine record_id from webhook — missing notes.record_id and entity.id")

    # --- failure reason ---
    error_code = (entity.get("error_code") or "").upper().replace(" ", "_")
    failure_reason = ERROR_CODE_MAP.get(error_code, FailureReason.BANK_DECLINE)

    # --- transaction type ---
    raw_type = notes.get("transaction_type", "one_time_payment")
    transaction_type = TRANSACTION_TYPE_MAP.get(raw_type, TransactionType.ONE_TIME_PAYMENT)

    # --- subscription / invoice fields (only if type warrants them) ---
    subscription_id = notes.get("subscription_id") if transaction_type == TransactionType.SUBSCRIPTION else None
    invoice_id      = notes.get("invoice_id")      if transaction_type == TransactionType.B2B_INVOICE   else None
    due_date_str    = notes.get("due_date")         if transaction_type == TransactionType.B2B_INVOICE   else None

    # Pydantic validator requires subscription_id for subscriptions and
    # invoice_id+due_date for B2B. Fallback to synthetic IDs so we can still
    # route and log — better than dropping the failure entirely.
    if transaction_type == TransactionType.SUBSCRIPTION and not subscription_id:
        subscription_id = f"sub-from-webhook-{record_id}"
    if transaction_type == TransactionType.B2B_INVOICE:
        if not invoice_id:
            invoice_id = f"inv-from-webhook-{record_id}"
        if not due_date_str:
            due_date_str = date.today().isoformat()  # conservative: treat as due today

    due_date = date.fromisoformat(due_date_str) if due_date_str else None

    return TransactionFailure(
        record_id                = record_id,
        transaction_type         = transaction_type,
        failure_reason           = failure_reason,
        amount_inr               = amount_inr,
        attempt_count            = int(notes.get("attempt_count", 1)),
        last_attempt_date        = date.today(),
        customer_name            = notes.get("customer_name") or entity.get("contact") or "Unknown",
        customer_language_pref   = notes.get("customer_language_pref", "en"),
        payment_method           = entity.get("method"),
        past_recovery_success    = notes.get("past_recovery_success", False),
        subscription_id          = subscription_id,
        invoice_id               = invoice_id,
        due_date                 = due_date,
        po_reference             = notes.get("po_reference"),
        counterparty_business_name = notes.get("counterparty_business_name"),
    )

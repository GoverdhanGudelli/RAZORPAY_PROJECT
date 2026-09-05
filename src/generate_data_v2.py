"""
Generates 100 synthetic transaction-failure records spanning:
- subscriptions (recurring payment failures)
- B2B invoices (overdue business payments)
- one-time payments (bank downtime, hard declines)

All records validated against schemas.TransactionFailure before being
written — guarantees the dataset itself can never violate the contract
the router expects. Bad records are surfaced, not silently dropped.
"""

import csv
import random
import sys
import os
from pathlib import Path
from datetime import date, timedelta
from faker import Faker

# ── Ensure src/ is on the path when called from the project root ────────────
SRC_DIR = Path(__file__).parent
ROOT_DIR = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from schemas import TransactionFailure, TransactionType, FailureReason

fake = Faker("en_IN")
random.seed(7)

LANGUAGE_PREF = ["en", "hi", "hinglish"]
PAYMENT_METHODS = ["upi_autopay", "credit_card", "debit_card", "netbanking"]

# distribution across transaction types
TYPE_WEIGHTS = {
    TransactionType.SUBSCRIPTION: 0.40,
    TransactionType.B2B_INVOICE: 0.25,
    TransactionType.ONE_TIME_PAYMENT: 0.25,
    TransactionType.CHECKOUT_ABANDONMENT: 0.10,
}

# which failure reasons are plausible for which transaction type
REASONS_BY_TYPE = {
    TransactionType.SUBSCRIPTION: [
        FailureReason.INSUFFICIENT_FUNDS, FailureReason.CARD_EXPIRED,
        FailureReason.BANK_DECLINE, FailureReason.THREE_DS_AUTH_FAIL,
        FailureReason.MANDATE_REVOKED,
    ],
    TransactionType.B2B_INVOICE: [
        FailureReason.INVOICE_OVERDUE, FailureReason.BANK_DECLINE,
    ],
    TransactionType.ONE_TIME_PAYMENT: [
        FailureReason.BANK_DOWNTIME, FailureReason.HARD_DECLINE,
        FailureReason.BANK_DECLINE, FailureReason.THREE_DS_AUTH_FAIL,
        FailureReason.INSUFFICIENT_FUNDS,
    ],
    TransactionType.CHECKOUT_ABANDONMENT: [
        FailureReason.CART_ABANDONED,
    ],
}

B2B_AMOUNTS = [15000, 25000, 50000, 75000, 120000, 250000]
SUB_AMOUNTS = [199, 299, 499, 999, 1499, 2499]
ONE_TIME_AMOUNTS = [499, 1200, 2999, 5999, 9999]


def random_date_within(days_back: int) -> date:
    return date.today() - timedelta(days=random.randint(0, days_back))


def generate_record(idx: int) -> TransactionFailure:
    txn_type = random.choices(
        list(TYPE_WEIGHTS.keys()), weights=list(TYPE_WEIGHTS.values()), k=1
    )[0]
    reason = random.choice(REASONS_BY_TYPE[txn_type])

    hard_fail = reason in (
        FailureReason.MANDATE_REVOKED, FailureReason.CARD_EXPIRED,
        FailureReason.HARD_DECLINE, FailureReason.INVOICE_OVERDUE,
    )
    attempt_count = random.randint(1, 3) if hard_fail else random.randint(0, 2)
    past_success = random.random() < (0.15 if hard_fail else 0.55)

    base = {
        "record_id": f"TXN{idx:04d}",
        "transaction_type": txn_type,
        "failure_reason": reason,
        "attempt_count": attempt_count,
        "last_attempt_date": random_date_within(14),
        "customer_name": fake.name(),
        "customer_language_pref": random.choice(LANGUAGE_PREF),
        "payment_method": random.choice(PAYMENT_METHODS),
        "past_recovery_success": past_success,
    }

    if txn_type == TransactionType.SUBSCRIPTION:
        base.update(
            amount_inr=random.choice(SUB_AMOUNTS),
            subscription_id=f"SUB{idx:04d}",
            days_past_due=random.randint(0, 14),
        )
    elif txn_type == TransactionType.B2B_INVOICE:
        due = date.today() - timedelta(days=random.randint(1, 45))
        base.update(
            amount_inr=random.choice(B2B_AMOUNTS),
            invoice_id=f"INV{idx:04d}",
            due_date=due,
            po_reference=f"PO-{random.randint(10000,99999)}",
            counterparty_business_name=fake.company(),
            payment_method=None,  # B2B invoices often not tied to a stored method
        )
    elif txn_type == TransactionType.CHECKOUT_ABANDONMENT:
        hours = random.choice([0, 2, 12, 30, 50])
        base.update(
            amount_inr=random.choice(ONE_TIME_AMOUNTS),
            checkout_session_id=f"order_cart_{idx:04d}",
            hours_since_checkout=hours,
            payment_method=None,
            attempt_count=0,
        )
    else:  # ONE_TIME_PAYMENT
        base.update(amount_inr=random.choice(ONE_TIME_AMOUNTS))
        # Sprinkle a few high-value one-time failures to trigger voice recovery
        if random.random() < 0.15:
            base["amount_inr"] = random.choice([12000, 25000, 45000])
            base["attempt_count"] = random.randint(1, 2)

    return TransactionFailure(**base)


def main(n: int = 100, out_path: str = None):
    if out_path is None:
        out_path = str(ROOT_DIR / "data" / "transactions.csv")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    records = []
    rejected = 0
    for i in range(1, n + 1):
        try:
            records.append(generate_record(i))
        except Exception as e:
            rejected += 1
            print(f"REJECTED record {i}: {e}")

    fieldnames = list(TransactionFailure.model_fields.keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            row = r.model_dump()
            # enums/dates -> plain strings for CSV
            for k, v in row.items():
                if hasattr(v, "value"):
                    row[k] = v.value
                elif hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
            writer.writerow(row)

    print(f"Generated {len(records)} valid records ({rejected} rejected) -> {out_path}")

    from collections import Counter
    type_counts = Counter(r.transaction_type.value for r in records)
    reason_counts = Counter(r.failure_reason.value for r in records)
    print("Transaction type distribution:", dict(type_counts))
    print("Failure reason distribution:", dict(reason_counts))


if __name__ == "__main__":
    main()

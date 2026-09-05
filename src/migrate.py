"""
migrate.py — One-time migration: existing CSV + JSONL → SQLite.

Run once from anywhere in the project:
    python src/migrate.py

Idempotent: rows that already exist are skipped (INSERT OR IGNORE semantics
implemented via session.get() check). Safe to re-run.

Import order:
  1. init_db()        — creates tables + view if not already present
  2. transactions.csv → transactions table
  3. audit_log_v2.jsonl → audit_log table
     (only imports entries whose record_id exists in transactions — any
      orphaned audit entries are skipped with a warning, not silently dropped)
"""

from __future__ import annotations
import csv
import json
import os
import sys
from datetime import date, datetime, timezone

# ---- path setup: make src/ importable from any CWD ----
_HERE = os.path.dirname(os.path.abspath(__file__))   # .../src/
_ROOT = os.path.dirname(_HERE)                        # .../revenue-recovery-agent/
sys.path.insert(0, _HERE)

from db.engine import init_db, get_session
from db.models import Transaction, AuditLog

CSV_PATH  = os.path.join(_ROOT, "data", "transactions.csv")
JSONL_PATH = os.path.join(_ROOT, "logs", "audit_log_v2.jsonl")


def migrate_transactions(session) -> tuple[int, int]:
    """Import data/transactions.csv → transactions table. Returns (imported, skipped)."""
    if not os.path.exists(CSV_PATH):
        print(f"  [skip] {CSV_PATH} not found — no transactions to import")
        return 0, 0

    imported = skipped = 0
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            clean = {k: (v if v != "" else None) for k, v in row.items()}
            record_id = clean["record_id"]

            # Skip if already present
            if session.get(Transaction, record_id) is not None:
                skipped += 1
                continue

            txn = Transaction(
                record_id                = record_id,
                transaction_type         = clean["transaction_type"],
                failure_reason           = clean["failure_reason"],
                amount_inr               = int(clean["amount_inr"]),
                attempt_count            = int(clean["attempt_count"]),
                last_attempt_date        = date.fromisoformat(clean["last_attempt_date"]),
                customer_name            = clean["customer_name"],
                customer_language_pref   = clean.get("customer_language_pref") or "en",
                payment_method           = clean.get("payment_method"),
                past_recovery_success    = clean.get("past_recovery_success") in ("True", "true", "1"),
                subscription_id          = clean.get("subscription_id"),
                invoice_id               = clean.get("invoice_id"),
                due_date                 = date.fromisoformat(clean["due_date"]) if clean.get("due_date") else None,
                po_reference             = clean.get("po_reference"),
                counterparty_business_name = clean.get("counterparty_business_name"),
            )
            session.add(txn)
            imported += 1

    return imported, skipped


def migrate_audit_log(session) -> tuple[int, int, int]:
    """
    Import logs/audit_log_v2.jsonl → audit_log table.
    Returns (imported, skipped_duplicate, skipped_orphan).
    Orphan = audit entry whose record_id has no matching transaction row.
    """
    if not os.path.exists(JSONL_PATH):
        print(f"  [skip] {JSONL_PATH} not found — no audit entries to import")
        return 0, 0, 0

    imported = skipped_dup = skipped_orphan = 0
    with open(JSONL_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            record_id = entry["record_id"]

            # Guard: only import if the parent transaction exists
            if session.get(Transaction, record_id) is None:
                print(f"  [warn] orphan audit entry — record_id={record_id} not in transactions table, skipping")
                skipped_orphan += 1
                continue

            # Dedup by (record_id, timestamp) — safe for the small existing logs
            ts = datetime.fromisoformat(entry["timestamp"])
            dup = (
                session.query(AuditLog)
                .filter(AuditLog.record_id == record_id, AuditLog.timestamp == ts)
                .first()
            )
            if dup is not None:
                skipped_dup += 1
                continue

            audit = AuditLog(
                timestamp       = ts,
                record_id       = record_id,
                transaction_type = entry["transaction_type"],
                failure_reason   = entry["failure_reason"],
                amount_inr       = entry["amount_inr"],
                attempt_count    = entry["attempt_count"],
                action_taken     = entry["action_taken"],
                reasoning        = entry["reasoning"],
                stop_after_this  = entry["stop_after_this"],
                execution        = entry.get("execution"),
            )
            session.add(audit)
            imported += 1

    return imported, skipped_dup, skipped_orphan


def verify(session) -> None:
    """Print counts and a sample from the recovery_metrics view."""
    from sqlalchemy import text
    txn_count   = session.query(Transaction).count()
    audit_count = session.query(AuditLog).count()
    print(f"\n  transactions : {txn_count} rows")
    print(f"  audit_log    : {audit_count} rows")

    rows = session.execute(text(
        "SELECT recovery_status, COUNT(*) as cnt, SUM(amount_inr) as total "
        "FROM recovery_metrics GROUP BY recovery_status"
    )).fetchall()
    if rows:
        print("\n  recovery_metrics VIEW:")
        print(f"  {'status':<16} {'count':>6}  {'amount_inr':>12}")
        print("  " + "-" * 38)
        for row in rows:
            print(f"  {row[0]:<16} {row[1]:>6}  {row[2]:>12}")
    else:
        print("\n  recovery_metrics VIEW: (empty — no audit entries yet)")


def main():
    print("=== Revenue Recovery Agent — DB Migration ===\n")
    init_db()

    with get_session() as session:
        print("Importing transactions.csv...")
        t_imp, t_skip = migrate_transactions(session)
        print(f"  imported={t_imp}  skipped={t_skip}")

        # Flush transactions first so FK checks pass for audit entries
        session.flush()

        print("\nImporting audit_log_v2.jsonl...")
        a_imp, a_dup, a_orphan = migrate_audit_log(session)
        print(f"  imported={a_imp}  skipped_duplicate={a_dup}  skipped_orphan={a_orphan}")

        print("\nVerification:")
        verify(session)

    print("\nMigration complete.")


if __name__ == "__main__":
    main()

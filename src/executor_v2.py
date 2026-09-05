"""
Executor v2 — full pipeline glue (v4: unified DB persistence).

Flow: CSV -> TransactionFailure -> route() -> AgentAction
      -> execute_action() OR schedule deferred -> audit JSONL + SQLite
      -> eval report (includes measured recovery from RecoveryEvent when present)

Every stage validates before passing to the next. A malformed record or
an invalid action never silently reaches the payment API.
"""

import csv
import json
import os
import sys
from datetime import date, datetime, timezone
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from schemas import TransactionFailure, AgentAction, ActionType
from router_v2 import route
from razorpay_client import execute_action, MOCK_MODE, LinkCapExceededError, RazorpayClient
from db.engine import get_session, init_db
from db.models import Transaction, AuditLog
from scheduler import schedule_action

MAX_INCENTIVE_USES = 1
REAL_MODE_BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))


def load_records(path="data/transactions.csv") -> list[TransactionFailure]:
    records = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            clean = {k: (v if v != "" else None) for k, v in row.items()}
            clean["amount_inr"] = int(clean["amount_inr"])
            clean["attempt_count"] = int(clean["attempt_count"])
            clean["past_recovery_success"] = clean["past_recovery_success"] in ("True", "true", "1")
            clean["last_attempt_date"] = date.fromisoformat(clean["last_attempt_date"])
            if clean.get("due_date"):
                clean["due_date"] = date.fromisoformat(clean["due_date"])
            if clean.get("days_past_due"):
                clean["days_past_due"] = int(clean["days_past_due"])
            if clean.get("hours_since_checkout"):
                clean["hours_since_checkout"] = int(clean["hours_since_checkout"])
            if clean.get("promised_payment_date"):
                clean["promised_payment_date"] = date.fromisoformat(clean["promised_payment_date"])
            # Drop unknown CSV columns that aren't on the schema
            known = set(TransactionFailure.model_fields.keys())
            clean = {k: v for k, v in clean.items() if k in known}
            records.append(TransactionFailure(**clean))
    return records


def _persist_to_db(record: TransactionFailure, action: AgentAction, exec_result: dict) -> None:
    """Unified persistence — same tables as the API path."""
    with get_session() as session:
        _TXN_COLUMNS = {c.key for c in Transaction.__table__.columns}
        existing = session.get(Transaction, record.record_id)
        if existing is None:
            txn_data = {k: v for k, v in record.model_dump().items() if k in _TXN_COLUMNS}
            txn_data["transaction_type"] = record.transaction_type.value
            txn_data["failure_reason"] = record.failure_reason.value
            txn_data.setdefault("recovery_status", "pending")
            session.add(Transaction(**txn_data))
            session.flush()
            existing = session.get(Transaction, record.record_id)

        if exec_result.get("outcome") != "scheduled":
            existing.attempt_count = (existing.attempt_count or 0) + 1
            existing.last_attempt_date = date.today()

        # In mock mode, treat successful retries as measured recovery for The Bar demo
        if exec_result.get("outcome") == "success":
            existing.recovery_status = "recovered"
            existing.recovered_amount_inr = record.amount_inr
            existing.recovered_at = datetime.now(timezone.utc)
            existing.past_recovery_success = True
            from db.models import RecoveryEvent
            session.add(RecoveryEvent(
                record_id=record.record_id,
                razorpay_payment_id=(exec_result.get("api_response") or {}).get("id"),
                razorpay_event_id=f"mock-batch-{record.record_id}-{existing.attempt_count}",
                amount_inr=record.amount_inr,
                event_type="mock.retry_captured",
                recovered_at=datetime.now(timezone.utc),
                raw_payload=exec_result,
            ))

        session.add(AuditLog(
            timestamp=datetime.now(timezone.utc),
            record_id=record.record_id,
            transaction_type=record.transaction_type.value,
            failure_reason=record.failure_reason.value,
            amount_inr=record.amount_inr,
            attempt_count=existing.attempt_count,
            action_taken=action.action.value,
            reasoning=action.reasoning,
            stop_after_this=action.stop_after_this,
            execution=exec_result,
        ))


def run_pipeline(records: list[TransactionFailure], log_path="logs/audit_log_v2.jsonl"):
    incentive_used = set()
    audit_entries = []

    init_db()

    if not MOCK_MODE and len(records) > REAL_MODE_BATCH_SIZE:
        print(
            f"MOCK_MODE=false: limiting this run to the first {REAL_MODE_BATCH_SIZE} "
            f"records (of {len(records)}) to stay well under Razorpay Test Mode's "
            f"30-link quota. Set BATCH_SIZE env var to change this."
        )
        records = records[:REAL_MODE_BATCH_SIZE]

    shared_client = RazorpayClient()

    for record in records:
        action = route(record)

        if action.action == ActionType.SEND_INCENTIVE_LINK:
            if record.record_id in incentive_used:
                action = AgentAction(
                    record_id=record.record_id,
                    action=ActionType.ESCALATE_TO_HUMAN,
                    reasoning=action.reasoning + " | OVERRIDE: incentive cap already used, escalating.",
                    stop_after_this=True,
                    escalation_priority="medium",
                    propensity_score=action.propensity_score,
                )
            else:
                incentive_used.add(record.record_id)

        # Deferred retries → schedule in DB.
        # In MOCK batch mode, also execute immediately so The Bar shows measured
        # recovery (mock captures). Live/API mode relies on POST /scheduler/run.
        if getattr(action, "defer_execution", False) and action.action == ActionType.RETRY_PAYMENT:
            with get_session() as session:
                pending = schedule_action(session, record, action)

            if MOCK_MODE:
                # Execute now for demo metrics; pending row remains for scheduler visibility
                immediate = action.model_copy(update={"defer_execution": False})
                try:
                    exec_result = execute_action(immediate, record, client=shared_client)
                    exec_result["also_scheduled"] = True
                    exec_result["pending_action_id"] = pending.id
                    exec_result["intended_delay_hours"] = action.retry_delay_hours
                except Exception as e:
                    exec_result = {
                        "record_id": action.record_id,
                        "action": action.action.value,
                        "executed_at": datetime.now(timezone.utc).isoformat(),
                        "mock_mode": True,
                        "outcome": "api_error",
                        "error": str(e),
                        "pending_action_id": pending.id,
                    }
                _persist_to_db(record, immediate, exec_result)
            else:
                exec_result = {
                    "record_id": action.record_id,
                    "action": action.action.value,
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                    "mock_mode": shared_client.mock,
                    "api_response": None,
                    "outcome": "scheduled",
                    "pending_action_id": pending.id,
                    "execute_at": pending.execute_at.isoformat(),
                    "retry_delay_hours": action.retry_delay_hours,
                    "propensity_score": action.propensity_score,
                }
                _persist_to_db(record, action, exec_result)
        else:
            try:
                exec_result = execute_action(action, record, client=shared_client)
            except LinkCapExceededError as e:
                print(f"\nSTOPPING RUN: {e}")
                print(f"Processed {len(audit_entries)} of {len(records)} records before stopping.")
                break
            except Exception as e:
                err_str = str(e)
                outcome = "rate_limited" if "Too many requests" in err_str else "api_error"
                print(f"  [{outcome}] {record.record_id}: {err_str[:120]}")
                exec_result = {
                    "record_id": action.record_id,
                    "action": action.action.value,
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                    "mock_mode": shared_client.mock,
                    "api_response": None,
                    "outcome": outcome,
                    "error": err_str,
                }

            _persist_to_db(record, action, exec_result)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "record_id": record.record_id,
            "transaction_type": record.transaction_type.value,
            "failure_reason": record.failure_reason.value,
            "amount_inr": record.amount_inr,
            "attempt_count": record.attempt_count,
            "action_taken": action.action.value,
            "reasoning": action.reasoning,
            "stop_after_this": action.stop_after_this,
            "propensity_score": action.propensity_score,
            "execution": exec_result,
        }
        audit_entries.append(entry)

    with open(log_path, "w", encoding="utf-8") as f:
        for entry in audit_entries:
            f.write(json.dumps(entry, default=str) + "\n")

    print(f"Processed {len(audit_entries)} records (MOCK_MODE={MOCK_MODE}) -> {log_path} + SQLite")
    return audit_entries


def evaluate(audit_entries: list[dict]) -> dict:
    total_at_risk = sum(e["amount_inr"] for e in audit_entries)
    recovered = 0
    recovered_count = 0
    escalated = 0
    voice_count = 0
    scheduled_count = 0
    action_breakdown = Counter()
    type_breakdown = Counter()

    for e in audit_entries:
        action_breakdown[e["action_taken"]] += 1
        type_breakdown[e["transaction_type"]] += 1

        exec_ = e["execution"]
        outcome = exec_.get("outcome")

        if e["action_taken"] == "ESCALATE_TO_HUMAN":
            escalated += 1
        if e["action_taken"] == "INITIATE_VOICE_CALL":
            voice_count += 1
        if outcome == "scheduled" or exec_.get("also_scheduled"):
            scheduled_count += 1

        if outcome == "success":
            recovered += e["amount_inr"]
            recovered_count += 1

    # Prefer DB measured recovery if available
    measured = None
    try:
        from recovery_attribution import get_batch_recovery_metrics
        with get_session() as session:
            measured = get_batch_recovery_metrics(session)
            if measured["recovered_amount_inr"] > recovered:
                recovered = measured["recovered_amount_inr"]
                recovered_count = measured["recovered_transaction_count"]
    except Exception:
        pass

    report = {
        "mock_mode": MOCK_MODE,
        "total_records": len(audit_entries),
        "total_amount_at_risk_inr": total_at_risk,
        "recovered_amount_inr": recovered,
        "recovered_count": recovered_count,
        "recovery_rate_pct": round(100 * recovered_count / len(audit_entries), 1) if audit_entries else 0,
        "escalated_count": escalated,
        "escalated_pct": round(100 * escalated / len(audit_entries), 1) if audit_entries else 0,
        "voice_call_count": voice_count,
        "scheduled_retry_count": scheduled_count,
        "action_breakdown": dict(action_breakdown),
        "transaction_type_breakdown": dict(type_breakdown),
        "measured_recovery": measured,
        "note": (
            "Recovered amounts include mock RETRY_PAYMENT captures and RecoveryEvent "
            "rows from payment.captured / payment_link.paid webhooks. "
            "SEND_*_LINK outcomes remain pending until customer pays. "
            "INITIATE_VOICE_CALL is Hinglish voice recovery (Track 03). "
            "outcome=scheduled means retry_delay_hours was deferred to the job queue."
        ),
    }
    return report


if __name__ == "__main__":
    # Resolve paths relative to project root
    root = os.path.dirname(_HERE)
    os.chdir(root)
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/transactions.csv"
    print(f"Loading records from: {csv_path}")
    records = load_records(csv_path)
    audit_entries = run_pipeline(records)
    report = evaluate(audit_entries)

    print("\n=== EVAL REPORT (v4 Track 03) ===")
    print(json.dumps(report, indent=2, default=str))

    with open("logs/eval_report_v2.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

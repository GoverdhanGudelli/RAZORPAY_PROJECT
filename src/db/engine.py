"""
db/engine.py — SQLAlchemy engine, session factory, and init_db().

DB location: data/recovery.db (relative to the project root, resolved via __file__
so this works regardless of which directory you run from).

recovery_metrics VIEW is created here via raw DDL because SQLAlchemy's ORM has
no built-in VIEW abstraction. It's a goal 4 requirement — separates:
  - 'captured'  = outcome='success'         (money actually recovered)
  - 'pending'   = link_sent/incentive_sent  (customer action still needed)
  - 'escalated' = action=ESCALATE_TO_HUMAN  (human queue)
  - 'no_action' = outcome='no_action_taken'
  - 'error'     = rate_limited / api_error  (infra failures, goal 6)

Uses SQLite JSON1 extension (json_extract) — bundled in Python's sqlite3 since 3.38+.
"""

from __future__ import annotations
import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

# ---- resolve DB path relative to this file, not the CWD ----
_HERE = os.path.dirname(os.path.abspath(__file__))   # .../src/db/
_SRC  = os.path.dirname(_HERE)                        # .../src/
_ROOT = os.path.dirname(_SRC)                         # .../revenue-recovery-agent/
DB_PATH = os.path.join(_ROOT, "data", "recovery.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,  # set True for SQL debug logging
)

# Enable FK enforcement — SQLite has it OFF by default
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that auto-commits on clean exit, rolls back on exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# SQL for the recovery_metrics VIEW.
# Separates goal 4 outcomes: captured ≠ link_sent ≠ escalated ≠ error ≠ voice ≠ scheduled.
_CREATE_METRICS_VIEW = text("""
    CREATE VIEW IF NOT EXISTS recovery_metrics AS
    SELECT
        a.id,
        a.record_id,
        a.timestamp,
        a.amount_inr,
        a.action_taken,
        json_extract(a.execution, '$.outcome')  AS outcome,
        json_extract(a.execution, '$.mock_mode') AS mock_mode,
        CASE
            WHEN json_extract(a.execution, '$.outcome') = 'success'
                THEN 'captured'
            WHEN EXISTS (
                SELECT 1 FROM recovery_events re WHERE re.record_id = a.record_id
            )
                THEN 'captured'
            WHEN json_extract(a.execution, '$.outcome') IN (
                'link_sent', 'incentive_link_sent',
                'checkout_recovery_link_sent', 'mandate_setup_link_sent'
            )
                THEN 'pending'
            WHEN json_extract(a.execution, '$.outcome') = 'voice_call_initiated'
                THEN 'voice_outreach'
            WHEN json_extract(a.execution, '$.outcome') = 'scheduled'
                THEN 'scheduled'
            WHEN a.action_taken = 'ESCALATE_TO_HUMAN'
                THEN 'escalated'
            WHEN json_extract(a.execution, '$.outcome') = 'no_action_taken'
                THEN 'no_action'
            WHEN json_extract(a.execution, '$.outcome') IN ('rate_limited', 'api_error', 'pending_or_failed')
                THEN 'error'
            ELSE 'unknown'
        END AS recovery_status
    FROM audit_log a
""")


def init_db() -> None:
    """
    Create all tables + the recovery_metrics VIEW if they don't exist.
    Safe to call multiple times (idempotent). Called at app startup and
    at the top of migrate.py.
    """
    from .models import Base  # local import avoids circular at module level
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        # Recreate view so schema upgrades pick up new CASE branches
        conn.execute(text("DROP VIEW IF EXISTS recovery_metrics"))
        conn.execute(_CREATE_METRICS_VIEW)
        # Additive column migrations for existing SQLite DBs
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(transactions)")).fetchall()}
        alters = []
        if "days_past_due" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN days_past_due INTEGER")
        if "checkout_session_id" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN checkout_session_id VARCHAR")
        if "hours_since_checkout" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN hours_since_checkout INTEGER")
        if "promised_payment_date" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN promised_payment_date DATE")
        if "recovery_status" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN recovery_status VARCHAR DEFAULT 'pending'")
        if "recovered_amount_inr" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN recovered_amount_inr INTEGER")
        if "recovered_at" not in cols:
            alters.append("ALTER TABLE transactions ADD COLUMN recovered_at DATETIME")
        for stmt in alters:
            conn.execute(text(stmt))
        conn.commit()
    print(f"DB initialised -> {DB_PATH}")

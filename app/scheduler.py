"""
Durable "do X at time T" scheduler.

Before this module the project had no way to act later: RETRY_LATER was only a
label (`pending_retry_in_24h`) and nothing ever fired. Mandate retries,
pre-debit notices, promise reminders/checks, the invoice chase ladder and voice
calls all need real future-dated work, so they share this one table
(`ScheduledAction`) and this one loop.

Rules this module enforces for every feature built on it:
- Exactly-once: a due row is claimed with an atomic
  UPDATE ... WHERE status='scheduled' (same idea as Order.revenue_recorded).
  Two scheduler passes - or two processes - can never both run the same job.
- Everything is audited (scheduled_action_created / _executed / _cancelled /
  _deferred), so the timeline is reconstructable from /audit/log.
- Contact guardrail, deliberately NOT tunable: customer-facing outreach only
  runs inside the 08:00-19:00 IST window (RBI fair-practices norms for
  recovery contact). A job that comes due outside it is deferred to the next
  window start rather than fired or dropped.

Handlers are registered per kind by the feature modules (mandate, promises,
receivables, voice). A kind with no handler is SKIPPED and audited, never
silently lost.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app import clock
from app.audit import write_audit_entry
from app.config import settings
from app.database import SessionLocal
from app.models import ScheduledAction, ScheduledKind, ScheduledStatus

logger = logging.getLogger(__name__)

# handler(db, action, payload) -> (final_status, result_label)
Handler = Callable[[Session, ScheduledAction, dict], tuple[str, str]]
_HANDLERS: dict[str, Handler] = {}

# Kinds that contact a customer and therefore respect the IST contact window.
CONTACT_KINDS = frozenset({
    ScheduledKind.PRE_DEBIT_NOTICE.value,
    ScheduledKind.PROMISE_REMINDER.value,
    ScheduledKind.INVOICE_CHASE.value,
    ScheduledKind.VOICE_CALL.value,
    ScheduledKind.ORDER_LINK_REMINDER.value,
})

_IST_OFFSET = timedelta(hours=5, minutes=30)
CONTACT_WINDOW_START_HOUR = 8    # inclusive, IST
CONTACT_WINDOW_END_HOUR = 19     # exclusive, IST


def register_handler(kind: ScheduledKind, handler: Handler) -> None:
    _HANDLERS[kind.value] = handler


# ── Contact window ───────────────────────────────────────────────────────────

def in_contact_window(now_utc: datetime) -> bool:
    ist = now_utc + _IST_OFFSET
    return CONTACT_WINDOW_START_HOUR <= ist.hour < CONTACT_WINDOW_END_HOUR


def next_contact_window_start(now_utc: datetime) -> datetime:
    """Next 08:00 IST at or after `now_utc`, returned as naive UTC."""
    ist = now_utc + _IST_OFFSET
    start = ist.replace(hour=CONTACT_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if ist >= start:
        start += timedelta(days=1)
    return start - _IST_OFFSET


# ── Public API ───────────────────────────────────────────────────────────────

def schedule(
    db: Session,
    kind: ScheduledKind,
    subject_type: str,
    subject_id: str,
    due_at: datetime,
    payload: dict | None = None,
    customer_ref: str | None = None,
    attempt_no: int = 1,
) -> ScheduledAction:
    row = ScheduledAction(
        kind=kind.value,
        subject_type=subject_type,
        subject_id=str(subject_id),
        customer_ref=customer_ref,
        due_at=due_at,
        status=ScheduledStatus.SCHEDULED.value,
        attempt_no=attempt_no,
        payload_json=json.dumps(payload or {}, default=str, sort_keys=True),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    write_audit_entry(db, "scheduled_action_created", {
        "scheduled_action_id": row.id,
        "kind": row.kind,
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "due_at": due_at.isoformat(),
        "attempt_no": attempt_no,
    })
    return row


def cancel_for_subject(
    db: Session,
    subject_type: str,
    subject_id: str,
    reason: str,
    kinds: list[ScheduledKind] | None = None,
) -> int:
    """Cancel every still-SCHEDULED job for a subject (invoice paid, promise
    opened, mandate halted...). CLAIMED/DONE rows are left alone - something is
    already running or has run. Returns how many were cancelled."""
    q = db.query(ScheduledAction).filter(
        ScheduledAction.subject_type == subject_type,
        ScheduledAction.subject_id == str(subject_id),
        ScheduledAction.status == ScheduledStatus.SCHEDULED.value,
    )
    if kinds:
        q = q.filter(ScheduledAction.kind.in_([k.value for k in kinds]))
    cancelled = 0
    for row in q.all():
        claimed = (
            db.query(ScheduledAction)
            .filter(ScheduledAction.id == row.id, ScheduledAction.status == ScheduledStatus.SCHEDULED.value)
            .update({"status": ScheduledStatus.CANCELLED.value, "result": f"cancelled:{reason}",
                     "executed_at": clock.now()})
        )
        db.commit()
        if claimed:
            cancelled += 1
            write_audit_entry(db, "scheduled_action_cancelled", {
                "scheduled_action_id": row.id,
                "kind": row.kind,
                "subject_type": subject_type,
                "subject_id": str(subject_id),
                "reason": reason,
            })
    return cancelled


# ── Execution ────────────────────────────────────────────────────────────────

def run_due_once(db: Session, now: datetime | None = None) -> dict:
    """One pass: fire everything due. Returns counts for logging/tests."""
    now = now or clock.now()
    counts = {"executed": 0, "deferred": 0, "skipped": 0, "failed": 0}

    due = (
        db.query(ScheduledAction)
        .filter(ScheduledAction.status == ScheduledStatus.SCHEDULED.value, ScheduledAction.due_at <= now)
        .order_by(ScheduledAction.due_at.asc())
        .limit(200)
        .all()
    )
    for row in due:
        # Contact guardrail BEFORE claiming, so a deferral never burns the claim.
        if row.kind in CONTACT_KINDS and not in_contact_window(now):
            new_due = next_contact_window_start(now)
            db.query(ScheduledAction).filter(
                ScheduledAction.id == row.id,
                ScheduledAction.status == ScheduledStatus.SCHEDULED.value,
            ).update({"due_at": new_due})
            db.commit()
            write_audit_entry(db, "scheduled_action_deferred", {
                "scheduled_action_id": row.id,
                "kind": row.kind,
                "reason": "outside_contact_window_ist_0800_1900",
                "new_due_at": new_due.isoformat(),
            })
            counts["deferred"] += 1
            continue

        claimed = (
            db.query(ScheduledAction)
            .filter(ScheduledAction.id == row.id, ScheduledAction.status == ScheduledStatus.SCHEDULED.value)
            .update({"status": ScheduledStatus.CLAIMED.value})
        )
        db.commit()
        if not claimed:
            continue   # another pass/process got it first

        db.refresh(row)
        handler = _HANDLERS.get(row.kind)
        if handler is None:
            _finish(db, row, ScheduledStatus.SKIPPED.value, "no_handler_registered")
            counts["skipped"] += 1
            continue

        try:
            payload = json.loads(row.payload_json or "{}")
            final_status, result = handler(db, row, payload)
        except Exception as e:   # a bad job must never kill the loop
            logger.exception("scheduled action %s (%s) failed", row.id, row.kind)
            _finish(db, row, ScheduledStatus.SKIPPED.value, f"handler_error:{type(e).__name__}")
            counts["failed"] += 1
            continue

        _finish(db, row, final_status, result)
        counts["executed"] += 1

    return counts


def _finish(db: Session, row: ScheduledAction, status: str, result: str) -> None:
    row.status = status
    row.result = result
    row.executed_at = clock.now()
    db.commit()
    write_audit_entry(db, "scheduled_action_executed", {
        "scheduled_action_id": row.id,
        "kind": row.kind,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "attempt_no": row.attempt_no,
        "status": status,
        "result": result,
    })


def run_scheduler() -> None:
    """Daemon loop, same shape as storefront.run_cart_idle_sweep."""
    interval = max(5, settings.scheduler_interval_seconds)
    while True:
        try:
            db = SessionLocal()
            try:
                run_due_once(db)
            finally:
                db.close()
        except Exception:
            logger.exception("scheduler pass failed; will retry next interval")
        time.sleep(interval)

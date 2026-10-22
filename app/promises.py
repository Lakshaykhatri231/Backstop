"""
Promise-to-pay tracker.

In collections, "I'll pay on Friday" is a formal record: it should pause the
chase (nobody wants a reminder the day after they promised), be checked on
the day, and - if it is broken - restart the chase one step firmer.

A promise is a STATUS OVERLAY on money that is already at risk. It moves no
rupee and has no bucket of its own (see models.PaymentPromise): the debt stays
where it was until a real payment releases it to recovered or a final give-up
releases it to lost. A broken promise therefore never books a loss by itself.

Subject kinds ("what is being promised against") plug in through three small
registries so this module knows nothing about mandates or invoices:
- register_subject(): resolve a subject -> amount, customer, still-open?, B2B?
- register_opened_hook(): what the subject does when a promise opens
- register_broken_hook(): what the subject does when a promise is broken

Dates are validated by rules_engine.validate_promise - one gate for customer,
merchant and voice (LLM-extracted) sources alike.
"""
import hashlib
import hmac
import logging
from datetime import date, datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import clock, runtime_flags
from app.actions import execute_action
from app.audit import write_audit_entry
from app.config import settings
from app.database import get_db
from app.merchant_auth import require_merchant
from app.models import (
    AgentAction, PaymentPromise, PromiseStatus, ScheduledAction, ScheduledKind, ScheduledStatus,
)
from app.rules_engine import validate_promise
from app.scheduler import cancel_for_subject, register_handler, schedule

logger = logging.getLogger(__name__)
router = APIRouter()

PROMISE_SUBJECT = "promise"
CAPTURE_SOURCES = {"voice", "self_serve_link", "merchant_manual"}
_IST = timedelta(hours=5, minutes=30)

# subject_type -> fn(db, subject_id) -> dict | None
#   {"amount_inr", "customer_ref", "is_open", "b2b", "chase_kinds": [ScheduledKind]}
_SUBJECTS: dict[str, Callable] = {}
_OPENED_HOOKS: dict[str, Callable] = {}   # fn(db, promise) -> None
_BROKEN_HOOKS: dict[str, Callable] = {}   # fn(db, promise) -> None


def register_subject(subject_type: str, resolver: Callable) -> None:
    _SUBJECTS[subject_type] = resolver


def register_opened_hook(subject_type: str, fn: Callable) -> None:
    _OPENED_HOOKS[subject_type] = fn


def register_broken_hook(subject_type: str, fn: Callable) -> None:
    _BROKEN_HOOKS[subject_type] = fn


def ist_today(now: datetime | None = None) -> date:
    return ((now or clock.now()) + _IST).date()


def _ten_am_ist_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 10, 0) - _IST


# ── Core ─────────────────────────────────────────────────────────────────────

def create_promise(
    db: Session,
    subject_type: str,
    subject_id: str,
    promised_date: date,
    captured_via: str,
    now: datetime | None = None,
) -> tuple[PaymentPromise | None, str, str]:
    """Validate and record a promise. Returns (promise|None, code, reason);
    code is "ok" or a rejection code from validate_promise / lookup."""
    now = now or clock.now()
    if captured_via not in CAPTURE_SOURCES:
        return None, "bad_source", f"captured_via must be one of {sorted(CAPTURE_SOURCES)}"
    resolver = _SUBJECTS.get(subject_type)
    info = resolver(db, subject_id) if resolver else None
    if info is None:
        return None, "unknown_subject", f"No {subject_type} with id {subject_id}."
    if not info["is_open"]:
        return None, "subject_closed", "That debt is already settled or closed."

    existing = db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == subject_type, PaymentPromise.subject_id == str(subject_id),
    ).all()
    ok, code, reason = validate_promise(
        promised_date=promised_date,
        today=ist_today(now),
        max_days=(runtime_flags.get_promise_max_days_b2b() if info["b2b"]
                  else runtime_flags.get_promise_max_days_b2c()),
        open_promise_exists=any(p.status == PromiseStatus.OPEN.value for p in existing),
        broken_count=sum(1 for p in existing if p.status == PromiseStatus.BROKEN.value),
        max_broken=runtime_flags.get_promise_max_broken(),
    )
    if not ok:
        write_audit_entry(db, "promise_rejected", {
            "subject_type": subject_type, "subject_id": str(subject_id),
            "promised_date": promised_date.isoformat(), "code": code, "reason": reason,
            "captured_via": captured_via,
        })
        if code == "too_many_broken":
            execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
            write_audit_entry(db, "promise_refused_escalated", {
                "subject_type": subject_type, "subject_id": str(subject_id),
            })
        return None, code, reason

    promise = PaymentPromise(
        subject_type=subject_type, subject_id=str(subject_id), customer_ref=info["customer_ref"],
        amount_inr=info["amount_inr"], promised_date=promised_date, captured_via=captured_via,
    )
    db.add(promise)
    db.commit()
    db.refresh(promise)
    write_audit_entry(db, "promise_opened", {
        "promise_id": promise.id, "subject_type": subject_type, "subject_id": str(subject_id),
        "amount_inr": promise.amount_inr, "promised_date": promised_date.isoformat(),
        "captured_via": captured_via,
    })

    # Pause the chase: nothing nags a customer who just gave a date.
    paused = cancel_for_subject(db, subject_type, subject_id, reason="promise_opened",
                                kinds=info.get("chase_kinds"))

    # Gentle reminder the day before, then the verdict a grace period after.
    reminder_at = _ten_am_ist_utc(promised_date - timedelta(days=1))
    if reminder_at > now:
        schedule(db, ScheduledKind.PROMISE_REMINDER, PROMISE_SUBJECT, promise.id, reminder_at,
                 payload={"subject_type": subject_type, "subject_id": str(subject_id)},
                 customer_ref=info["customer_ref"])
    check_at = _ten_am_ist_utc(promised_date + timedelta(days=runtime_flags.get_promise_grace_days()))
    schedule(db, ScheduledKind.PROMISE_CHECK, PROMISE_SUBJECT, promise.id, check_at,
             payload={"subject_type": subject_type, "subject_id": str(subject_id)},
             customer_ref=info["customer_ref"])

    hook = _OPENED_HOOKS.get(subject_type)
    if hook:
        hook(db, promise)
    _keep_link_alive(db, subject_type, subject_id, check_at)
    logger.info("promise %s opened (paused %d chase jobs)", promise.id, paused)
    return promise, "ok", "Promise accepted."


def _keep_link_alive(db: Session, subject_type: str, subject_id: str, check_at: datetime) -> None:
    """The debt's open Payment Link must still work on the promised day: extend it
    (same link, no new Test Mode slot) rather than let it lapse and be replaced.
    Link times are real time, so the wait is measured on the app clock and added
    to the real now."""
    if subject_type not in ("invoice", "mandate_run"):
        return
    try:
        from app import payment_links
        wait = max(check_at - clock.now(), timedelta(0))
        payment_links.extend_for_promise(db, subject_type, str(subject_id),
                                         datetime.utcnow() + wait + timedelta(days=1))
    except Exception:                      # a link problem must never undo a promise
        logger.exception("could not extend the payment link for %s %s", subject_type, subject_id)


def resolve_on_payment(db: Session, subject_type: str, subject_id: str, amount_paid: float) -> bool:
    """Called by whichever path confirmed a payment on a subject. Marks the
    open promise KEPT (or PARTIALLY_KEPT) and drops its pending jobs. Moves no
    money - the payment path already did."""
    promise = db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == subject_type, PaymentPromise.subject_id == str(subject_id),
        PaymentPromise.status == PromiseStatus.OPEN.value,
    ).first()
    if promise is None:
        return False
    claimed = (
        db.query(PaymentPromise)
        .filter(PaymentPromise.id == promise.id, PaymentPromise.status == PromiseStatus.OPEN.value)
        .update({
            "status": (PromiseStatus.KEPT.value if amount_paid >= promise.amount_inr - 0.01
                       else PromiseStatus.PARTIALLY_KEPT.value),
            "amount_paid": amount_paid, "resolved_at": clock.now(),
        })
    )
    db.commit()
    if not claimed:
        return False
    db.refresh(promise)
    cancel_for_subject(db, PROMISE_SUBJECT, promise.id, reason=f"promise_{promise.status}")
    write_audit_entry(db, "promise_kept" if promise.status == PromiseStatus.KEPT.value
                      else "promise_partially_kept", {
        "promise_id": promise.id, "subject_type": subject_type, "subject_id": str(subject_id),
        "amount_inr": promise.amount_inr, "amount_paid": amount_paid,
    })
    return True


def close_for_subject(db: Session, subject_type: str, subject_id: str, reason: str) -> bool:
    """The debt was given up on (lost/written off): an open promise on it can
    no longer be kept or broken, so retire it. Moves no money."""
    promise = db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == subject_type, PaymentPromise.subject_id == str(subject_id),
        PaymentPromise.status == PromiseStatus.OPEN.value,
    ).first()
    if promise is None:
        return False
    claimed = (
        db.query(PaymentPromise)
        .filter(PaymentPromise.id == promise.id, PaymentPromise.status == PromiseStatus.OPEN.value)
        .update({"status": PromiseStatus.CANCELLED.value, "resolved_at": clock.now()})
    )
    db.commit()
    if not claimed:
        return False
    cancel_for_subject(db, PROMISE_SUBJECT, promise.id, reason=reason)
    write_audit_entry(db, "promise_cancelled", {"promise_id": promise.id, "reason": reason})
    return True


# ── Scheduler handlers ───────────────────────────────────────────────────────

def _open_promise(db: Session, action: ScheduledAction) -> PaymentPromise | None:
    p = db.query(PaymentPromise).filter(PaymentPromise.id == action.subject_id).first()
    return p if p and p.status == PromiseStatus.OPEN.value else None


def _handle_reminder(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    promise = _open_promise(db, action)
    if promise is None:
        return ScheduledStatus.SKIPPED.value, "promise_not_open"
    from app import messaging, notify   # local: keeps this module import-light
    msg = notify.promise_reminder(db, promise)
    sent = msg is not None and messaging.delivered(msg)
    write_audit_entry(db, "promise_reminder_sent" if sent else "promise_reminder_not_sent", {
        "promise_id": promise.id, "promised_date": promise.promised_date.isoformat(),
        "success": sent, "message_id": msg.id if msg else None,
        "message_status": msg.status if msg else "no_subject",
    })
    return ScheduledStatus.DONE.value, ("reminder_sent" if sent
                                        else f"reminder_not_sent:{msg.status if msg else 'no_subject'}")


def _handle_check(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    promise = _open_promise(db, action)
    if promise is None:
        return ScheduledStatus.SKIPPED.value, "promise_not_open"
    claimed = (
        db.query(PaymentPromise)
        .filter(PaymentPromise.id == promise.id, PaymentPromise.status == PromiseStatus.OPEN.value)
        .update({"status": PromiseStatus.BROKEN.value, "resolved_at": clock.now()})
    )
    db.commit()
    if not claimed:
        return ScheduledStatus.SKIPPED.value, "promise_not_open"
    db.refresh(promise)
    write_audit_entry(db, "promise_broken", {
        "promise_id": promise.id, "subject_type": promise.subject_type,
        "subject_id": promise.subject_id, "promised_date": promise.promised_date.isoformat(),
        "amount_inr": promise.amount_inr,
    })
    hook = _BROKEN_HOOKS.get(promise.subject_type)
    if hook:
        hook(db, promise)   # the subject decides how to get one step firmer
    return ScheduledStatus.DONE.value, "promise_broken"


register_handler(ScheduledKind.PROMISE_REMINDER, _handle_reminder)
register_handler(ScheduledKind.PROMISE_CHECK, _handle_check)


# ── Self-serve link token ────────────────────────────────────────────────────

def make_promise_token(subject_type: str, subject_id: str) -> str:
    """Unforgeable per-subject token for the customer's pay-link page. No
    login: the link itself is the credential, scoped to exactly one debt."""
    msg = f"promise:{subject_type}:{subject_id}".encode()
    return hmac.new(settings.storefront_secret_key.encode(), msg, hashlib.sha256).hexdigest()[:32]


# ── API ──────────────────────────────────────────────────────────────────────

class PromiseIn(BaseModel):
    subject_type: str
    subject_id: str
    promised_date: date


class SelfServePromiseIn(PromiseIn):
    token: str


def promise_state(p: PaymentPromise, today: date | None = None, grace_days: int | None = None) -> dict:
    """Where a promise stands RIGHT NOW, in words a merchant can act on.

    The stored status only says open/kept/broken. An open promise is really
    one of: still ahead, due today, or past its date but inside the grace
    window (it has not been judged yet). Judging happens at the check job on
    promised_date + grace days, so "the date arrived" is not "it was missed"."""
    today = today or ist_today()
    grace = runtime_flags.get_promise_grace_days() if grace_days is None else grace_days
    d = p.promised_date
    check_on = d + timedelta(days=grace)
    days_left = (d - today).days
    fmt = lambda x: x.strftime("%d %b")   # noqa: E731
    paid = f"₹{p.amount_paid:,.0f}"
    if p.status == PromiseStatus.KEPT.value:
        return {"state": "kept", "days_left": days_left, "check_on": check_on.isoformat(),
                "label": f"Kept: the payment arrived (promised for {fmt(d)})."}
    if p.status == PromiseStatus.PARTIALLY_KEPT.value:
        return {"state": "partly_kept", "days_left": days_left, "check_on": check_on.isoformat(),
                "label": f"Partly kept: {paid} of ₹{p.amount_inr:,.0f} was paid by the date."}
    if p.status == PromiseStatus.BROKEN.value:
        return {"state": "broken", "days_left": days_left, "check_on": check_on.isoformat(),
                "label": (f"Broken: no payment by {fmt(d)}. Automatic retries stopped and the debt "
                          f"went to a person." if p.subject_type == "mandate_run"
                          else f"Broken: no payment by {fmt(d)}. Chasing resumed one step firmer.")}
    if p.status == PromiseStatus.CANCELLED.value:
        return {"state": "cancelled", "days_left": days_left, "check_on": check_on.isoformat(),
                "label": "Cancelled."}
    if days_left > 0:
        return {"state": "waiting", "days_left": days_left, "check_on": check_on.isoformat(),
                "label": f"Waiting: due in {days_left} day{'s' if days_left != 1 else ''}, on {fmt(d)}. "
                         f"Chasing is paused until then."}
    if days_left == 0:
        return {"state": "due_today", "days_left": 0, "check_on": check_on.isoformat(),
                "label": f"Due today ({fmt(d)}). Not paid yet. If no payment arrives it is marked "
                         f"broken at the check on {fmt(check_on)} (after the {grace}-day grace)."}
    return {"state": "in_grace", "days_left": days_left, "check_on": check_on.isoformat(),
            "label": f"The date ({fmt(d)}) has passed and no payment has arrived. It is marked broken "
                     f"at the check on {fmt(check_on)}; a payment before then still keeps it."}


def _out(p: PaymentPromise) -> dict:
    return {**promise_state(p), 
        "id": p.id, "subject_type": p.subject_type, "subject_id": p.subject_id,
        "customer_ref": p.customer_ref, "amount_inr": p.amount_inr,
        "promised_date": p.promised_date.isoformat(), "captured_via": p.captured_via,
        "status": p.status, "amount_paid": p.amount_paid,
        "created_at": p.created_at, "resolved_at": p.resolved_at,
    }


def _create_or_400(db: Session, req: PromiseIn, via: str) -> dict:
    promise, code, reason = create_promise(db, req.subject_type, req.subject_id, req.promised_date, via)
    if promise is None:
        status = 404 if code == "unknown_subject" else 409 if code in ("already_open", "subject_closed") else 422
        raise HTTPException(status_code=status, detail={"code": code, "reason": reason})
    return _out(promise)


@router.post("/promises", dependencies=[Depends(require_merchant)])
def create_manual(req: PromiseIn, db: Session = Depends(get_db)):
    """Merchant keyed in a promise they took over the phone/WhatsApp."""
    return _create_or_400(db, req, "merchant_manual")


@router.post("/promises/self-serve")
def create_self_serve(req: SelfServePromiseIn, db: Session = Depends(get_db)):
    """Customer picked a date on their pay-link page."""
    expected = make_promise_token(req.subject_type, req.subject_id)
    if not hmac.compare_digest(expected, req.token):
        raise HTTPException(status_code=403, detail="Invalid promise link")
    return _create_or_400(db, req, "self_serve_link")


@router.get("/promises/link-token", dependencies=[Depends(require_merchant)])
def link_token(subject_type: str, subject_id: str):
    """Merchant helper: the token to embed in a customer's pay-link."""
    return {"subject_type": subject_type, "subject_id": subject_id,
            "token": make_promise_token(subject_type, subject_id),
            "link_path": f"/pay-later?st={subject_type}&sid={subject_id}"
                         f"&t={make_promise_token(subject_type, subject_id)}"}


@router.post("/promises/{promise_id}/cancel", dependencies=[Depends(require_merchant)])
def cancel_promise(promise_id: str, db: Session = Depends(get_db)):
    promise = db.query(PaymentPromise).filter(PaymentPromise.id == promise_id).first()
    if promise is None:
        raise HTTPException(status_code=404, detail="Promise not found")
    claimed = (
        db.query(PaymentPromise)
        .filter(PaymentPromise.id == promise_id, PaymentPromise.status == PromiseStatus.OPEN.value)
        .update({"status": PromiseStatus.CANCELLED.value, "resolved_at": clock.now()})
    )
    db.commit()
    if not claimed:
        raise HTTPException(status_code=409, detail="Promise is not open")
    cancel_for_subject(db, PROMISE_SUBJECT, promise_id, reason="promise_cancelled")
    write_audit_entry(db, "promise_cancelled", {"promise_id": promise_id})
    db.refresh(promise)
    return _out(promise)


@router.get("/merchant/promises", dependencies=[Depends(require_merchant)])
def list_promises(db: Session = Depends(get_db)):
    rows = db.query(PaymentPromise).order_by(PaymentPromise.created_at.desc()).limit(200).all()
    by_status: dict[str, int] = {}
    by_source: dict[str, dict] = {}
    for p in rows:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        if p.status in (PromiseStatus.KEPT.value, PromiseStatus.PARTIALLY_KEPT.value,
                        PromiseStatus.BROKEN.value):
            s = by_source.setdefault(p.captured_via, {"resolved": 0, "kept": 0})
            s["resolved"] += 1
            if p.status != PromiseStatus.BROKEN.value:
                s["kept"] += 1
    return {
        "promises": [_out(p) for p in rows],
        "counts": by_status,
        # Kept-rate by how the promise was captured - the number that tells a
        # merchant which channel produces promises that actually get paid.
        "kept_rate_by_source": {
            k: {**v, "kept_rate": round(v["kept"] / v["resolved"], 3)} for k, v in by_source.items()
        },
    }

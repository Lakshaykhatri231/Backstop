"""
B2B receivables chaser: the fourth signal source (overdue invoices).

The consumer pipelines react to a checkout going wrong. Here nothing "fails":
a business client simply pays an invoice late, and someone in finance has to
remember who, send the emails, make the call and escalate. This module does
that on a schedule, firmer step by step, and stops the moment a client either
pays, promises a date, or disputes the invoice.

Money (separate buckets in the SAME audited ledger, never in total_at_risk or
the consumer recovery rate - see models.MerchantRevenueState):
    due_at passes      -> receivables_overdue += outstanding   (once)
    client pays        -> overdue -> receivables_collected     (partial ok)
    write-off / credit -> overdue -> receivables_written_off
An invoice that is not yet due is not "at risk". Invoices are never lost by a
timer: B2B debts are written off by a person.

Chase ladder (rules_engine.rule_based_receivable_decision judges each step AT
FIRE TIME, so a promise or dispute that appears after scheduling is honoured):
    promise opens  -> ladder PAUSES (steps cancelled); a broken promise
                      resumes it one step firmer (promises broken-hook)
    dispute raised -> ladder STOPS; only a human restarts it (/resume)

Chase messages go through messaging.py; the payment link is a real Razorpay
Payment Link (app/payment_links.py: created with the first message that needs
it, reused, cancelled when the invoice settles) or a labelled stand-in.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import clock, contacts as contacts_service, otp as otp_service, payment_links, promises, runtime_flags
from app.actions import execute_action
from app.audit import write_audit_entry
from app.database import get_db
from app.models import (
    AgentAction, BusinessAccount, Invoice, InvoiceStatus, PaymentPromise, PromiseStatus,
    ScheduledAction, ScheduledKind, ScheduledStatus,
)
from app.revenue import adjust as adjust_revenue
from app.rules_engine import ESCALATE_STEP, rule_based_receivable_decision
from app.scheduler import cancel_for_subject, register_handler, schedule

logger = logging.getLogger(__name__)
router = APIRouter()

SUBJECT = "invoice"
_IST = timedelta(hours=5, minutes=30)
_LIVE = (InvoiceStatus.OPEN.value, InvoiceStatus.OVERDUE.value,
         InvoiceStatus.PARTIALLY_PAID.value, InvoiceStatus.DISPUTED.value)
_VOICE_DISPATCHER: Callable | None = None


def register_voice_dispatcher(fn: Callable) -> None:
    """fn(db, invoice, account) -> (success, outcome). Registered by voice.py."""
    global _VOICE_DISPATCHER
    _VOICE_DISPATCHER = fn


def _outstanding(inv: Invoice) -> float:
    return max(0.0, round(inv.amount_inr - inv.amount_paid, 2))


def _ten_am_ist(d) -> datetime:
    return datetime(d.year, d.month, d.day, 10, 0) - _IST


# ── Payment link ─────────────────────────────────────────────────────────────

def invoice_link(db: Session, inv: Invoice) -> "PaymentLink":
    """The Razorpay Payment Link for what is still owed, created the first time
    a message needs it (not at invoice creation - Test Mode caps links at ~30,
    and an invoice due in a month should not spend one) and reused after that.
    Part-payments are allowed; a credit note changes the amount, so the next
    message gets a fresh link and the old one is cancelled."""
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
    escalate_at = inv.due_at + timedelta(days=runtime_flags.get_receivables_escalate_after_days())
    expire_at = max(escalate_at, datetime.utcnow()) + timedelta(days=30)
    outstanding = _outstanding(inv)
    link = payment_links.ensure_link(
        db, SUBJECT, inv.id, outstanding, expire_at=expire_at,
        description=f"Invoice {inv.invoice_no}", accept_partial=True,
        customer={"name": (acct.contact_name or acct.company_name) if acct else None,
                  "email": acct.email if acct else None, "contact": acct.phone if acct else None})
    # mirrored for the dashboard and older readers
    inv.razorpay_payment_link_id, inv.payment_link_url = link.razorpay_link_id, link.short_url
    db.commit()
    return link


# ── Ladder scheduling ────────────────────────────────────────────────────────

def _ladder_dates(inv: Invoice) -> list[tuple[int, datetime]]:
    due_day = (inv.due_at + _IST).date()
    offsets = sorted(runtime_flags.get_receivables_chase_offsets())
    steps = [(i, _ten_am_ist(due_day + timedelta(days=off))) for i, off in enumerate(offsets)]
    steps.append((ESCALATE_STEP, _ten_am_ist(due_day + timedelta(
        days=runtime_flags.get_receivables_escalate_after_days()))))
    return steps


def _done_steps(db: Session, inv: Invoice) -> set[int]:
    rows = db.query(ScheduledAction).filter(
        ScheduledAction.subject_type == SUBJECT, ScheduledAction.subject_id == inv.id,
        ScheduledAction.kind == ScheduledKind.INVOICE_CHASE.value,
        ScheduledAction.status == ScheduledStatus.DONE.value,
    ).all()
    out = set()
    for r in rows:
        try:
            out.add(int(json.loads(r.payload_json).get("step")))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return out


def _schedule_ladder(db: Session, inv: Invoice, now: datetime, resume: bool = False) -> int:
    """Schedule every ladder step not already executed. Fresh invoices keep
    their calendar dates and skip steps already in the past. A resume (broken
    promise / dispute resolved) fires the first remaining step immediately and
    spaces the rest at least a day apart, so one resume never means a burst
    of five messages."""
    done = _done_steps(db, inv)
    ladder = _ladder_dates(inv)
    pending = [(i, d) for i, d in ladder if i not in done]
    if resume:
        # "One step firmer than where it stopped" is measured from where the
        # invoice actually IS on the ladder, not only from steps that fired:
        # a step can be due-but-never-sent (deferred overnight, or cancelled
        # when a promise opened). Restarting such an invoice at the courtesy
        # reminder, weeks past due, would be absurd. So the resume point is
        # the later of (the step its age has reached) and (last sent + 1).
        reached = max((i for i, d in ladder if d <= now), default=-1)
        first = max(reached, max(done, default=-1) + 1, 0)
        pending = [(i, d) for i, d in pending if i >= first]
    # never double-schedule a step that is still queued
    queued = set()
    for r in db.query(ScheduledAction).filter(
        ScheduledAction.subject_type == SUBJECT, ScheduledAction.subject_id == inv.id,
        ScheduledAction.kind == ScheduledKind.INVOICE_CHASE.value,
        ScheduledAction.status == ScheduledStatus.SCHEDULED.value,
    ).all():
        try:
            queued.add(int(json.loads(r.payload_json).get("step")))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    count = 0
    todo = [p for p in pending if p[0] not in queued]
    if not resume:
        # A back-dated import is already past some steps. Skipping them all
        # would leave an overdue client untouched until the next future step,
        # and firing them all would be a burst - so exactly ONE catch-up (the
        # latest missed step) fires now, then the future steps keep their dates.
        past = [p for p in todo if p[1] <= now]
        todo = ([(past[-1][0], now)] if past else []) + [p for p in todo if p[1] > now]
    for k, (step, date_at) in enumerate(todo):
        if resume:
            due = now if k == 0 else max(date_at, now + timedelta(days=k))
        else:
            due = date_at
        schedule(db, ScheduledKind.INVOICE_CHASE, SUBJECT, inv.id, due,
                 payload={"step": step, "invoice_no": inv.invoice_no},
                 customer_ref=inv.account_id, attempt_no=step + 1)
        count += 1
    return count


# ── Lifecycle ────────────────────────────────────────────────────────────────

def create_invoice(db: Session, account: BusinessAccount, invoice_no: str, amount_inr: float,
                   issued_at: datetime | None = None, due_at: datetime | None = None) -> Invoice:
    now = clock.now()
    issued_at = issued_at or now
    due_at = due_at or issued_at + timedelta(days=account.payment_terms_days)
    inv = Invoice(account_id=account.id, invoice_no=invoice_no, amount_inr=round(amount_inr, 2),
                  issued_at=issued_at, due_at=due_at)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    # No payment link yet: invoice_link() makes it when the first chase
    # message goes out (lazy - see app/payment_links.py).
    write_audit_entry(db, "invoice_created", {
        "invoice_id": inv.id, "invoice_no": invoice_no, "account_id": account.id,
        "amount_inr": inv.amount_inr, "due_at": due_at.isoformat(),
    })
    _schedule_ladder(db, inv, now)
    if due_at <= now:
        _flag_overdue(db, inv)   # back-dated import: already overdue
    return inv


def _flag_overdue(db: Session, inv: Invoice) -> bool:
    """Book the outstanding amount into receivables_overdue, exactly once."""
    claimed = (
        db.query(Invoice)
        .filter(Invoice.id == inv.id, Invoice.overdue_booked.is_(False), Invoice.settled.is_(False),
                Invoice.status.in_([InvoiceStatus.OPEN.value, InvoiceStatus.PARTIALLY_PAID.value]))
        .update({"overdue_booked": True})
    )
    db.commit()
    if not claimed:
        return False
    db.refresh(inv)
    amount = _outstanding(inv)
    inv.overdue_booked_amount = amount
    inv.status = InvoiceStatus.OVERDUE.value
    db.commit()
    if amount > 0:
        adjust_revenue(db, "receivables_overdue", amount, reason="invoice_overdue", order_id=inv.invoice_no)
    write_audit_entry(db, "invoice_overdue", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "outstanding_inr": amount,
    })
    return True


def flag_overdue_invoices(db: Session, now: datetime | None = None) -> int:
    """Maintenance-sweep job: invoices whose due date has passed."""
    now = now or clock.now()
    rows = db.query(Invoice).filter(
        Invoice.due_at < now, Invoice.overdue_booked.is_(False), Invoice.settled.is_(False),
        Invoice.status.in_([InvoiceStatus.OPEN.value, InvoiceStatus.PARTIALLY_PAID.value]),
    ).all()
    return sum(1 for inv in rows if _flag_overdue(db, inv))


def _release(db: Session, inv: Invoice, amount: float, bucket: str, reason: str) -> float:
    """Move up to `amount` of this invoice's booked overdue money to
    collected / written_off. Bounded by what was actually booked, so it can
    never release more than entered."""
    if not inv.overdue_booked:
        return 0.0
    remaining = max(0.0, inv.overdue_booked_amount - inv.overdue_released)
    move = round(min(amount, remaining), 2)
    if move <= 0:
        return 0.0
    adjust_revenue(db, "receivables_overdue", -move, reason=reason, order_id=inv.invoice_no)
    adjust_revenue(db, bucket, move, reason=reason, order_id=inv.invoice_no)
    inv.overdue_released = round(inv.overdue_released + move, 2)
    return move


def _finalize(db: Session, inv: Invoice, status: str) -> bool:
    """Final exit - exactly once."""
    claimed = (
        db.query(Invoice).filter(Invoice.id == inv.id, Invoice.settled.is_(False))
        .update({"settled": True, "status": status})
    )
    db.commit()
    if not claimed:
        return False
    db.refresh(inv)
    cancel_for_subject(db, SUBJECT, inv.id, reason=f"invoice_{status}")
    promises.close_for_subject(db, SUBJECT, inv.id, reason=f"invoice_{status}")
    # a settled invoice must not stay payable at the old amount
    payment_links.close_for_subject(db, SUBJECT, inv.id, reason=f"invoice_{status}")
    return True


def record_payment(db: Session, invoice_id: str, amount_inr: float, payment_id: str | None) -> dict:
    """Apply a client payment (payment-link webhook or manually keyed bank
    transfer). Payment-level dedupe on payment_id; partial payments allowed.
    Books no total_revenue: the payment.captured Razorpay sends alongside a
    payment-link payment already does that - same rule as mandate.py."""
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    if inv is None:
        return {"status": "unknown_invoice"}
    seen = json.loads(inv.paid_payment_ids or "[]")
    if payment_id and payment_id in seen:
        db.rollback()
        return {"status": "duplicate_payment_ignored", "invoice_id": inv.id}
    amount = round(float(amount_inr), 2)
    if amount <= 0:
        db.rollback()
        return {"status": "ignored_non_positive"}

    if inv.status == InvoiceStatus.WRITTEN_OFF.value:
        # Money arrived after the write-off was booked. The ledger exit
        # already happened; do not reverse it silently - surface it.
        inv.amount_paid = round(inv.amount_paid + amount, 2)
        if payment_id:
            inv.paid_payment_ids = json.dumps(seen + [payment_id])
        db.commit()
        write_audit_entry(db, "invoice_payment_after_write_off", {
            "invoice_id": inv.id, "invoice_no": inv.invoice_no, "amount_inr": amount})
        return {"status": "payment_after_write_off", "invoice_id": inv.id}

    inv.amount_paid = round(min(inv.amount_inr, inv.amount_paid + amount), 2)
    if payment_id:
        inv.paid_payment_ids = json.dumps(seen + [payment_id])
    full = _outstanding(inv) <= 0.01
    if full:
        # release EVERYTHING still booked, so rounding can't strand a paisa
        moved = _release(db, inv, inv.overdue_booked_amount, "receivables_collected", "invoice_paid")
        inv.paid_at = clock.now()
    else:
        moved = _release(db, inv, amount, "receivables_collected", "invoice_part_paid")
        if inv.status in (InvoiceStatus.OPEN.value,):
            inv.status = InvoiceStatus.PARTIALLY_PAID.value
    db.commit()
    write_audit_entry(db, "invoice_paid" if full else "invoice_partially_paid", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "payment_id": payment_id,
        "amount_inr": amount, "amount_paid_total": inv.amount_paid,
        "outstanding_inr": _outstanding(inv), "moved_to_collected": moved,
    })
    if full:
        _finalize(db, inv, InvoiceStatus.PAID.value)
    promises.resolve_on_payment(db, SUBJECT, inv.id, amount)
    return {"status": "paid" if full else "partially_paid", "invoice_id": inv.id,
            "outstanding_inr": _outstanding(inv)}


def dispute(db: Session, inv: Invoice, reason: str) -> bool:
    if inv.status not in (InvoiceStatus.OPEN.value, InvoiceStatus.OVERDUE.value,
                          InvoiceStatus.PARTIALLY_PAID.value) or inv.settled:
        return False
    claimed = (
        db.query(Invoice)
        .filter(Invoice.id == inv.id, Invoice.settled.is_(False),
                Invoice.status.in_([InvoiceStatus.OPEN.value, InvoiceStatus.OVERDUE.value,
                                    InvoiceStatus.PARTIALLY_PAID.value]))
        .update({"status": InvoiceStatus.DISPUTED.value})
    )
    db.commit()
    if not claimed:
        return False
    cancel_for_subject(db, SUBJECT, inv.id, reason="invoice_disputed")
    promises.close_for_subject(db, SUBJECT, inv.id, reason="invoice_disputed")
    # Disputed money is not chased; /resume makes a fresh link with the next message.
    payment_links.close_for_subject(db, SUBJECT, inv.id, reason="invoice_disputed")
    execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
    write_audit_entry(db, "invoice_disputed", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "reason": reason,
        "outstanding_inr": _outstanding(inv),
    })
    return True


def resume(db: Session, inv: Invoice) -> bool:
    """A human resolved the dispute in the merchant's favour: restart chasing."""
    claimed = (
        db.query(Invoice)
        .filter(Invoice.id == inv.id, Invoice.status == InvoiceStatus.DISPUTED.value)
        .update({"status": (InvoiceStatus.OVERDUE.value if inv.overdue_booked else
                            InvoiceStatus.PARTIALLY_PAID.value if inv.amount_paid > 0 else
                            InvoiceStatus.OPEN.value)})
    )
    db.commit()
    if not claimed:
        return False
    db.refresh(inv)
    n = _schedule_ladder(db, inv, clock.now(), resume=True)
    write_audit_entry(db, "invoice_chase_resumed", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "steps_scheduled": n})
    return True


def write_off(db: Session, inv: Invoice, reason: str) -> bool:
    if inv.settled:
        return False
    moved = _release(db, inv, inv.overdue_booked_amount, "receivables_written_off", "invoice_written_off")
    db.commit()
    if not _finalize(db, inv, InvoiceStatus.WRITTEN_OFF.value):
        return False
    write_audit_entry(db, "invoice_written_off", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "reason": reason,
        "written_off_inr": moved, "outstanding_inr": _outstanding(inv)})
    return True


def credit_note(db: Session, inv: Invoice, credit_inr: float, reason: str) -> dict:
    """Reduce what is owed (partly-right dispute). The credited part leaves
    the overdue bucket as written-off - it is revenue the merchant gave up."""
    if inv.settled:
        return {"status": "invoice_closed"}
    credit = round(min(credit_inr, _outstanding(inv)), 2)
    if credit <= 0:
        return {"status": "nothing_to_credit"}
    inv.amount_inr = round(inv.amount_inr - credit, 2)
    moved = _release(db, inv, credit, "receivables_written_off", "invoice_credit_note")
    db.commit()
    # The open link still asks for the old amount; close it now (not at the
    # next message) so the client cannot overpay in between.
    payment_links.close_for_subject(db, SUBJECT, inv.id, reason="invoice_credit_note")
    write_audit_entry(db, "invoice_credit_note", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "credit_inr": credit,
        "reason": reason, "written_off_inr": moved, "new_amount_inr": inv.amount_inr})
    if _outstanding(inv) <= 0.01:
        _finalize(db, inv, InvoiceStatus.PAID.value if inv.amount_paid > 0
                  else InvoiceStatus.WRITTEN_OFF.value)
    return {"status": "credited", "credit_inr": credit, "outstanding_inr": _outstanding(inv)}


# ── Scheduler handler: one chase step ────────────────────────────────────────

def _send_invoice_message(db: Session, inv: Invoice, template: str) -> tuple[bool, str]:
    from app import messaging, notify   # local: notify imports contacts/selfserve
    msg = notify.invoice_message(db, inv, template, outstanding=_outstanding(inv))
    ok = messaging.delivered(msg)
    label = "reminder_sent" if template == "invoice_courtesy" else "payment_link_sent"
    return ok, label if ok else f"{label.replace('_sent', '')}_not_sent:{msg.status}"


def _handle_chase(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    inv = db.query(Invoice).filter(Invoice.id == action.subject_id).first()
    if inv is None or inv.settled:
        return ScheduledStatus.SKIPPED.value, "invoice_closed"
    step = int(payload.get("step", 0))
    open_promise = db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == SUBJECT, PaymentPromise.subject_id == inv.id,
        PaymentPromise.status == PromiseStatus.OPEN.value).first() is not None
    act, code, reasoning = rule_based_receivable_decision(
        step=step, outstanding=_outstanding(inv),
        high_value=float(runtime_flags.get_high_value_amount_inr()),
        open_promise=open_promise, disputed=inv.status == InvoiceStatus.DISPUTED.value,
    )
    if act is None:
        return ScheduledStatus.SKIPPED.value, f"paused:{code}"

    account = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
    if act == AgentAction.VOICE_CALL:
        # The dispatcher (voice.place_call) re-judges verified-number +
        # customer-ticked consent + window at this moment. If it declines,
        # the step is not lost: the same message goes out as a payment link.
        success, outcome = False, "voice_unavailable"
        if _VOICE_DISPATCHER and account:
            success, outcome = _VOICE_DISPATCHER(db, inv, account)
        if not success:
            reason = outcome
            success, outcome = _send_invoice_message(db, inv, "invoice_overdue")
            outcome = f"voice_skipped({reason}):{outcome}"
    elif act == AgentAction.ESCALATE_TO_HUMAN:
        success, outcome = execute_action(act.value, None)
        inv.escalated_at = clock.now()
        db.commit()
    elif act == AgentAction.SEND_REMINDER:
        success, outcome = _send_invoice_message(db, inv, "invoice_courtesy")
    elif act == AgentAction.SEND_PAYMENT_LINK:
        success, outcome = _send_invoice_message(db, inv, "invoice_overdue")
    else:
        success, outcome = execute_action(act.value, None)

    write_audit_entry(db, "invoice_chase_executed", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "step": step, "code": code,
        "action": act.value, "outcome": outcome, "success": success, "reasoning": reasoning,
        "outstanding_inr": _outstanding(inv),
    })
    return ScheduledStatus.DONE.value, outcome


register_handler(ScheduledKind.INVOICE_CHASE, _handle_chase)


# ── Promise-to-pay integration ───────────────────────────────────────────────

def _promise_subject(db: Session, invoice_id: str) -> dict | None:
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv is None:
        return None
    return {
        "amount_inr": _outstanding(inv), "customer_ref": inv.account_id,
        "is_open": (not inv.settled) and inv.status in (
            InvoiceStatus.OPEN.value, InvoiceStatus.OVERDUE.value, InvoiceStatus.PARTIALLY_PAID.value),
        "b2b": True,
        "chase_kinds": [ScheduledKind.INVOICE_CHASE, ScheduledKind.VOICE_CALL],
    }


def _on_promise_broken(db: Session, promise) -> None:
    inv = db.query(Invoice).filter(Invoice.id == promise.subject_id).first()
    if inv is None or inv.settled or inv.status == InvoiceStatus.DISPUTED.value:
        return
    n = _schedule_ladder(db, inv, clock.now(), resume=True)
    write_audit_entry(db, "invoice_chase_resumed", {
        "invoice_id": inv.id, "invoice_no": inv.invoice_no, "steps_scheduled": n,
        "reason": "promise_broken", "promise_id": promise.id})


promises.register_subject(SUBJECT, _promise_subject)
promises.register_broken_hook(SUBJECT, _on_promise_broken)


# ── Razorpay payment-link webhook ────────────────────────────────────────────

def handle_payment_link_event(db: Session, event_name: str, inner_payload: dict) -> dict:
    """Legacy path: links created before app/payment_links.py existed are not
    in the registry, only on Invoice.razorpay_payment_link_id."""
    link = inner_payload.get("payment_link", {}).get("entity", {})
    payment = inner_payload.get("payment", {}).get("entity", {})
    link_id = link.get("id")
    inv = db.query(Invoice).filter(Invoice.razorpay_payment_link_id == link_id).first() if link_id else None
    if inv is None:
        write_audit_entry(db, "payment_link_event_unmatched", {"event": event_name, "payment_link_id": link_id})
        return {"status": "processed", "matched_invoice": False}
    # The specific payment's amount, not the link's running total.
    amount = (payment.get("amount") or 0) / 100.0
    result = record_payment(db, inv.id, amount, payment.get("id"))
    return {"matched_invoice": True, **result}


def _on_link_payment(db: Session, link, amount_inr: float, payment_id: str | None) -> dict:
    return {"matched_invoice": True, **record_payment(db, link.subject_id, amount_inr, payment_id)}


def _on_link_closed(db: Session, link, status: str) -> None:
    # Expired on Razorpay: the next chase message makes a fresh one.
    inv = db.query(Invoice).filter(Invoice.id == link.subject_id).first()
    if inv is not None and inv.razorpay_payment_link_id == link.razorpay_link_id:
        inv.razorpay_payment_link_id, inv.payment_link_url = None, None
        db.commit()


payment_links.register_link_handler(SUBJECT, _on_link_payment, _on_link_closed)


# ── API ──────────────────────────────────────────────────────────────────────

class AccountIn(BaseModel):
    company_name: str
    gstin: str | None = None
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    preferred_language: str = "hinglish"   # contacts.DEFAULT_LANGUAGE
    voice_consent: bool = False
    payment_terms_days: int = 30
    credit_limit_inr: float | None = None


class InvoiceIn(BaseModel):
    account_id: str
    invoice_no: str
    amount_inr: float
    issued_at: datetime | None = None
    due_at: datetime | None = None


class BulkInvoicesIn(BaseModel):
    invoices: list[InvoiceIn]


class PaymentIn(BaseModel):
    amount_inr: float
    payment_id: str | None = None


class ReasonIn(BaseModel):
    reason: str = ""


class CreditIn(BaseModel):
    amount_inr: float
    reason: str = ""


def _inv_out(inv: Invoice, now: datetime | None = None) -> dict:
    now = now or clock.now()
    outstanding = _outstanding(inv)
    return {
        "id": inv.id, "invoice_no": inv.invoice_no, "account_id": inv.account_id,
        "company_name": inv.account.company_name if inv.account else None,
        "amount_inr": inv.amount_inr, "amount_paid": inv.amount_paid, "outstanding_inr": outstanding,
        "status": inv.status, "issued_at": inv.issued_at, "due_at": inv.due_at,
        "days_past_due": max(0, (now - inv.due_at).days) if (not inv.settled and inv.due_at < now) else 0,
        "payment_link_url": inv.payment_link_url, "escalated_at": inv.escalated_at,
        "paid_at": inv.paid_at,
        # the customer's own page: verify number, pick a date, give consent
        "self_serve_path": __import__("app.selfserve", fromlist=["link_path"]).link_path(SUBJECT, inv.id),
    }


def _get_invoice(db: Session, invoice_id: str) -> Invoice:
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


@router.post("/receivables/accounts")
def create_account(req: AccountIn, db: Session = Depends(get_db)):
    if req.preferred_language not in ("en", "hi", "hinglish"):
        raise HTTPException(422, "preferred_language must be one of en, hi, hinglish")
    # voice_consent is accepted for backwards compatibility but IGNORED: the
    # merchant can supply a number and language, never the client's consent
    # (contacts.py). The client verifies the number and ticks consent
    # themselves on the self-serve link.
    # Check the number BEFORE the account exists: a bad number used to fail after the row was
    # committed and leave a half-made business behind on every retry.
    norm_phone = None
    if req.phone:
        try:
            norm_phone = otp_service.normalize_phone(req.phone)
        except otp_service.OtpError as e:
            raise HTTPException(422, detail={"code": e.code, "message": e.message})
    data = req.model_dump(exclude={"voice_consent", "phone", "preferred_language"})
    acct = BusinessAccount(**data, phone=req.phone, preferred_language=req.preferred_language,
                           voice_consent=False)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    if norm_phone:
        contacts_service.upsert_contact(
            db, business_account_id=acct.id, phone=norm_phone, language=req.preferred_language)
    write_audit_entry(db, "business_account_created", {"account_id": acct.id, "company_name": acct.company_name})
    return {"id": acct.id, "company_name": acct.company_name}


@router.get("/receivables/accounts")
def list_accounts(db: Session = Depends(get_db)):
    out = []
    for a in db.query(BusinessAccount).order_by(BusinessAccount.created_at.desc()).all():
        c = contacts_service.contact_out(contacts_service.for_business_account(db, a.id))
        out.append({
            "id": a.id, "company_name": a.company_name, "gstin": a.gstin,
            "contact_name": a.contact_name, "email": a.email,
            "phone_masked": c["phone_masked"], "phone_verified": c["phone_verified"],
            "preferred_language": c["preferred_language"], "voice_consent": c["voice_consent"],
            "can_call": c["can_call"], "payment_terms_days": a.payment_terms_days,
        })
    return out


def _create_one(db: Session, req: InvoiceIn) -> Invoice:
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == req.account_id).first()
    if acct is None:
        raise HTTPException(404, f"Account {req.account_id} not found")
    if req.amount_inr <= 0:
        raise HTTPException(422, "amount_inr must be positive")
    if db.query(Invoice).filter(Invoice.invoice_no == req.invoice_no).first():
        raise HTTPException(409, f"Invoice {req.invoice_no} already exists")
    return create_invoice(db, acct, req.invoice_no, req.amount_inr, req.issued_at, req.due_at)


@router.post("/receivables/invoices")
def create_invoice_endpoint(req: InvoiceIn, db: Session = Depends(get_db)):
    return _inv_out(_create_one(db, req))


@router.post("/receivables/invoices/bulk")
def create_invoices_bulk(req: BulkInvoicesIn, db: Session = Depends(get_db)):
    """CSV-import target: the dashboard parses the file client-side and posts
    the rows here. Per-row results, so one bad row never blocks the rest."""
    results = []
    for row in req.invoices:
        try:
            results.append({"invoice_no": row.invoice_no, "status": "created",
                            "id": _create_one(db, row).id})
        except HTTPException as e:
            results.append({"invoice_no": row.invoice_no, "status": "rejected", "detail": e.detail})
    return {"results": results}


# ── CSV import (browser parses; these two endpoints preview and commit) ─────
#
# The browser turns a finance export into rows carrying a company_name and/or
# gstin (never an internal account_id). /import-preview resolves every row
# WITHOUT creating anything, so the user sees "312 new, 40 already exist, 12
# rejected, 5 new companies" before they confirm - an import mints a payment
# link and a chase ladder per invoice, so a mistake is not cheaply undone.

IMPORT_ROW_CAP = 2000
_GSTIN_RE = __import__("re").compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


class ImportRow(BaseModel):
    invoice_no: str
    amount_inr: float
    account_id: str | None = None
    company_name: str | None = None
    gstin: str | None = None
    issued_at: datetime | None = None
    due_at: datetime | None = None


class ImportIn(BaseModel):
    rows: list[ImportRow]
    create_missing_accounts: bool = False


def _norm_name(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def _resolve_import_row(db: Session, row: ImportRow, accounts: list[BusinessAccount],
                        seen_invoice_nos: set[str], existing_nos: set[str]) -> dict:
    """Judge one row. Pure w.r.t. the DB (reads only)."""
    problems: list[str] = []
    inv_no = (row.invoice_no or "").strip()
    gstin = (row.gstin or "").strip().upper() or None
    out = {"invoice_no": inv_no, "amount_inr": row.amount_inr, "status": "new",
           "account_id": None, "company_name": (row.company_name or "").strip() or None,
           "account": "matched", "problems": problems}

    if not inv_no:
        problems.append("missing_invoice_no")
    if not row.amount_inr or row.amount_inr <= 0:
        problems.append("amount_must_be_positive")
    if gstin and not _GSTIN_RE.match(gstin):
        problems.append("invalid_gstin")
    if row.issued_at and row.due_at and row.due_at < row.issued_at:
        problems.append("due_before_issued")

    # account: explicit id, else GSTIN, else exact company name
    acct = None
    if row.account_id:
        acct = next((a for a in accounts if a.id == row.account_id), None)
        if acct is None:
            problems.append("unknown_account_id")
    elif gstin:
        acct = next((a for a in accounts if (a.gstin or "").upper() == gstin), None)
    if acct is None and not row.account_id and out["company_name"]:
        same = [a for a in accounts if _norm_name(a.company_name) == _norm_name(out["company_name"])]
        if len(same) > 1:
            problems.append("ambiguous_company_name")
        elif same:
            acct = same[0]
    if acct is not None:
        out["account_id"] = acct.id
        out["company_name"] = acct.company_name
    elif not row.account_id and not problems_has(problems, "ambiguous_company_name"):
        if out["company_name"]:
            out["account"] = "new_account"
        else:
            out["account"] = "unmatched"
            problems.append("no_company_or_gstin")

    if inv_no and inv_no in seen_invoice_nos:
        out["status"] = "duplicate_in_file"
        problems.append("duplicate_in_file")
    elif inv_no and inv_no in existing_nos:
        out["status"] = "exists"
    if inv_no:
        seen_invoice_nos.add(inv_no)

    if problems and out["status"] == "new":
        out["status"] = "rejected"
    return out


def problems_has(problems: list[str], code: str) -> bool:
    return code in problems


def _preview_rows(db: Session, rows: list[ImportRow]) -> tuple[list[dict], dict]:
    accounts = db.query(BusinessAccount).all()
    nos = [(r.invoice_no or "").strip() for r in rows if (r.invoice_no or "").strip()]
    existing = {i.invoice_no for i in db.query(Invoice.invoice_no).filter(Invoice.invoice_no.in_(nos)).all()} \
        if nos else set()
    seen: set[str] = set()
    results = [_resolve_import_row(db, r, accounts, seen, existing) for r in rows]
    counts = {"new": 0, "exists": 0, "rejected": 0, "duplicate_in_file": 0}
    for r in results:
        counts[r["status"]] += 1
    new_accounts = sorted({r["company_name"] for r in results
                           if r["status"] == "new" and r["account"] == "new_account" and r["company_name"]})
    return results, {"counts": counts, "new_accounts": new_accounts, "total": len(rows)}


@router.post("/receivables/invoices/import-preview")
def import_preview(req: ImportIn, db: Session = Depends(get_db)):
    if len(req.rows) > IMPORT_ROW_CAP:
        raise HTTPException(413, f"At most {IMPORT_ROW_CAP} rows per import; split the file.")
    results, summary = _preview_rows(db, req.rows)
    return {"rows": results, **summary}


@router.post("/receivables/invoices/import")
def import_invoices(req: ImportIn, db: Session = Depends(get_db)):
    """Commit. Rows the preview would have called rejected/exists/duplicate are
    reported, never created. Re-posting the same file is safe: invoice_no is
    the idempotency key. Companies not matched to an account are created only
    when create_missing_accounts is true (the UI asks once, listing them)."""
    if len(req.rows) > IMPORT_ROW_CAP:
        raise HTTPException(413, f"At most {IMPORT_ROW_CAP} rows per import; split the file.")
    previewed, summary = _preview_rows(db, req.rows)
    created_accounts: dict[str, BusinessAccount] = {}
    out = []
    for row, judged in zip(req.rows, previewed):
        entry = {"invoice_no": judged["invoice_no"], "status": judged["status"],
                 "problems": judged["problems"]}
        if judged["status"] != "new":
            out.append(entry)
            continue
        try:
            acct = None
            if judged["account_id"]:
                acct = db.query(BusinessAccount).filter(BusinessAccount.id == judged["account_id"]).first()
            elif judged["account"] == "new_account":
                if not req.create_missing_accounts:
                    entry.update(status="rejected", problems=["account_not_found_and_creation_not_confirmed"])
                    out.append(entry)
                    continue
                key = _norm_name(judged["company_name"])
                acct = created_accounts.get(key)
                if acct is None:
                    acct = BusinessAccount(company_name=judged["company_name"],
                                           gstin=(row.gstin or "").strip().upper() or None)
                    db.add(acct)
                    db.commit()
                    db.refresh(acct)
                    created_accounts[key] = acct
                    write_audit_entry(db, "business_account_created", {
                        "account_id": acct.id, "company_name": acct.company_name, "via": "csv_import"})
            inv = create_invoice(db, acct, judged["invoice_no"], row.amount_inr, row.issued_at, row.due_at)
            entry.update(status="created", id=inv.id)
        except Exception as e:   # one bad row must never sink the batch
            db.rollback()
            logger.exception("import row failed: %s", judged["invoice_no"])
            entry.update(status="rejected", problems=[f"error:{type(e).__name__}"])
        out.append(entry)
    write_audit_entry(db, "invoice_import_completed", {
        "rows": len(req.rows),
        "created": sum(1 for e in out if e["status"] == "created"),
        "accounts_created": len(created_accounts),
    })
    return {"results": out, "accounts_created": [a.company_name for a in created_accounts.values()],
            **{k: v for k, v in summary.items() if k != "new_accounts"}}


@router.get("/receivables/invoices")
def list_invoices(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Invoice)
    if status:
        q = q.filter(Invoice.status == status)
    now = clock.now()
    return [_inv_out(i, now) for i in q.order_by(Invoice.due_at.asc()).limit(500).all()]


@router.post("/receivables/invoices/{invoice_id}/payment")
def record_manual_payment(invoice_id: str, req: PaymentIn, db: Session = Depends(get_db)):
    """A payment received outside the payment link (bank transfer, cheque)."""
    _get_invoice(db, invoice_id)
    return record_payment(db, invoice_id, req.amount_inr, req.payment_id)


@router.post("/receivables/invoices/{invoice_id}/dispute")
def dispute_endpoint(invoice_id: str, req: ReasonIn, db: Session = Depends(get_db)):
    if not dispute(db, _get_invoice(db, invoice_id), req.reason):
        raise HTTPException(409, "Invoice cannot be disputed in its current state")
    return {"status": "disputed"}


@router.post("/receivables/invoices/{invoice_id}/resume")
def resume_endpoint(invoice_id: str, db: Session = Depends(get_db)):
    if not resume(db, _get_invoice(db, invoice_id)):
        raise HTTPException(409, "Invoice is not disputed")
    return {"status": "resumed"}


@router.post("/receivables/invoices/{invoice_id}/write-off")
def write_off_endpoint(invoice_id: str, req: ReasonIn, db: Session = Depends(get_db)):
    if not write_off(db, _get_invoice(db, invoice_id), req.reason):
        raise HTTPException(409, "Invoice is already closed")
    return {"status": "written_off"}


@router.post("/receivables/invoices/{invoice_id}/credit-note")
def credit_note_endpoint(invoice_id: str, req: CreditIn, db: Session = Depends(get_db)):
    return credit_note(db, _get_invoice(db, invoice_id), req.amount_inr, req.reason)


@router.get("/receivables/summary")
def summary(db: Session = Depends(get_db)):
    now = clock.now()
    live = db.query(Invoice).filter(Invoice.settled.is_(False), Invoice.status.in_(_LIVE)).all()
    aging = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    counts = {k: 0 for k in aging}
    not_yet_due = 0.0
    for inv in live:
        out = _outstanding(inv)
        if inv.due_at >= now:
            not_yet_due += out
            continue
        d = (now - inv.due_at).days
        key = "0-30" if d <= 30 else "31-60" if d <= 60 else "61-90" if d <= 90 else "90+"
        aging[key] += out
        counts[key] += 1
    paid = db.query(Invoice).filter(Invoice.status == InvoiceStatus.PAID.value,
                                    Invoice.paid_at.isnot(None)).all()
    days = [(i.paid_at - i.issued_at).total_seconds() / 86400 for i in paid]
    from app.revenue import get_or_create_state
    st = get_or_create_state(db)
    return {
        "aging": {k: {"amount_inr": round(v, 2), "invoices": counts[k]} for k, v in aging.items()},
        "not_yet_due_inr": round(not_yet_due, 2),
        "overdue_inr": round(st.receivables_overdue, 2),
        "collected_inr": round(st.receivables_collected, 2),
        "written_off_inr": round(st.receivables_written_off, 2),
        "avg_days_to_collect": round(sum(days) / len(days), 1) if days else None,
        "disputed": sum(1 for i in live if i.status == InvoiceStatus.DISPUTED.value),
    }

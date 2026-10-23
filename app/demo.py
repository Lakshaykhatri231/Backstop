"""
Demo Console backend (/demo/*).

Purpose: let someone WATCH the recovery flows work end to end without a real
SMS vendor, telephony or a wait of days - so the project can be verified, not
just described. Everything here drives the same code paths production would:

- the message outbox (simulated provider) is the "phone" - each notice, link
  and OTP the system sends is stored in outbound_messages and shown here;
- the demo clock (app/clock.py) moves "now" forward, so 25 hours later happens
  in one click while the real scheduler, contact-window guard and retry maths
  run unchanged;
- interactive calls ring in the browser: the real script, the real guards
  (verified number, customer-ticked consent, DND, window, caps), the real
  classifier and router - only the person on the line is whoever is at the page;
- scenarios create a failed debit / overdue invoice by the same internal path a
  webhook or import would. They never fabricate consent or verification:
  the "customer" has to do that on the real self-serve page.

Not available in production (every route 404s), and nothing here bypasses a
guardrail - the clock is the only thing it moves.
"""
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import memory as memory_mod, memory_retrieval, persona, speech, voice_chat
from app import clock, contacts, demo_text, mandate, messaging, receivables, revenue, runtime_flags, voice
from app import autopay, customer_profile, demo_personas, payment_links, razorpay_client as rz
from app import promises as promises_mod
from app.audit import write_audit_entry
from app.config import settings
from app.database import get_db
from app.models import (
    AuditLog, BusinessAccount, Event, EventType, Invoice, Mandate, MandateRun, Order, OrderStatus, OutboundMessage,
    PaymentLink, PaymentPromise,
    ScheduledAction, ScheduledKind, ScheduledStatus, VoiceCall, VoiceTurn,
)
from app.otp import OtpError, normalize_phone
from app.rules_engine import CARD_TOKEN_DEBIT_LEAD
from app.scheduler import (
    CONTACT_KINDS, in_contact_window, next_contact_window_start, run_due_once,
)
from app.selfserve import link_path

_IST = timedelta(hours=5, minutes=30)


def _not_in_production() -> None:
    if settings.environment == "production":
        raise HTTPException(404, "Not found")


router = APIRouter(prefix="/demo", dependencies=[Depends(_not_in_production)])


# ── State ────────────────────────────────────────────────────────────────────

def _clock_out() -> dict:
    n = clock.now()
    ist = n + _IST
    return {"now_utc": n.isoformat(), "now_ist": ist.strftime("%d %b %Y, %H:%M"),
            "offset_hours": clock.offset_hours(), "shifted": clock.is_shifted(),
            "in_contact_window": in_contact_window(n)}


@router.get("/state")
def state(db: Session = Depends(get_db)):
    pending = db.query(ScheduledAction).filter(
        ScheduledAction.status == ScheduledStatus.SCHEDULED.value).count()
    return {
        "environment": settings.environment,
        "clock": _clock_out(),
        "providers": {
            "otp": settings.otp_provider,
            "messaging": messaging.provider_name(),
            "voice": "browser (interactive)" if voice.interactive_mode() else "simulated (canned replies)",
            "dnd": settings.dnd_provider,
        },
        "interactive_calls": voice.interactive_mode(),
        "call_latency": voice_chat.latency_stats(db),
        "scheduled_pending": pending,
        "messages_total": db.query(OutboundMessage).count(),
        "ringing_calls": db.query(VoiceCall).filter(VoiceCall.status == "in_progress").count(),
        # Real Razorpay Test Mode links vs labelled stand-ins (the ~30-link cap).
        "razorpay_links": payment_links.usage(db, include_account=True),
        "modes": _modes(),
    }


# ── Inbox ────────────────────────────────────────────────────────────────────

def _msg_out(m: OutboundMessage) -> dict:
    return {"id": m.id, "template": m.template, "subject_type": m.subject_type,
            "subject_id": m.subject_id, "to_phone": m.to_phone, "language": m.language,
            "body": m.body, "link": m.link, "link_kind": m.link_kind, "provider": m.provider,
            "status": m.status, "error": m.error, "created_at": m.created_at}


@router.get("/messages")
def messages(limit: int = 100, db: Session = Depends(get_db)):
    rows = (db.query(OutboundMessage).order_by(OutboundMessage.created_at.desc())
            .limit(min(max(limit, 1), 300)).all())
    return [_msg_out(m) for m in rows]


# ── Scheduled jobs ───────────────────────────────────────────────────────────

@router.get("/scheduled")
def scheduled(limit: int = 60, db: Session = Depends(get_db)):
    up = (db.query(ScheduledAction).filter(ScheduledAction.status == ScheduledStatus.SCHEDULED.value)
          .order_by(ScheduledAction.due_at.asc()).limit(limit).all())
    done = (db.query(ScheduledAction).filter(ScheduledAction.status != ScheduledStatus.SCHEDULED.value)
            .order_by(ScheduledAction.executed_at.desc()).limit(limit).all())

    def out(r: ScheduledAction) -> dict:
        return {"id": r.id, "kind": r.kind, "subject_type": r.subject_type, "subject_id": r.subject_id,
                "due_at": r.due_at, "status": r.status, "result": r.result,
                "executed_at": r.executed_at, "attempt_no": r.attempt_no,
                "plain": demo_text.KIND_PLAIN.get(r.kind, r.kind.replace("_", " "))}
    return {"upcoming": [out(r) for r in up], "recent": [out(r) for r in done]}


# ── Clock ────────────────────────────────────────────────────────────────────

def _run_passes(db: Session) -> dict:
    """Fire everything due, repeatedly - a job's handler may schedule another
    that is already due (a callback, a promise check)."""
    total = {"executed": 0, "deferred": 0, "skipped": 0, "failed": 0}
    for _ in range(8):
        c = run_due_once(db)
        for k in total:
            total[k] += c[k]
        if c["executed"] == 0 and c["failed"] == 0:
            break
    swept = _sweep_money(db)
    total["lapsed"] = swept["lapsed_runs"]
    return total


def _sweep_money(db: Session) -> dict:
    """The money-side half of the maintenance sweep (give-up and overdue
    flagging). In real life a timer does this; with a moved clock it has to
    run when the clock moves, or "it moves to Lost on <date>" never happens."""
    return {"lapsed_runs": mandate.lapse_stale_runs(db),
            "invoices_flagged": receivables.flag_overdue_invoices(db)}


def _after(db: Session, moved_hours: float, counts: dict, note: str = "") -> dict:
    return {"moved_hours": round(moved_hours, 2), "ran": counts, "note": note, "clock": _clock_out()}


class AdvanceIn(BaseModel):
    hours: float


@router.post("/clock/advance")
def advance(req: AdvanceIn, db: Session = Depends(get_db)):
    if req.hours <= 0 or req.hours > 24 * 45:
        raise HTTPException(422, "hours must be between 0 and 1080 (45 days)")
    clock.advance(req.hours)
    write_audit_entry(db, "demo_clock_advanced", {"hours": req.hours, "offset_hours": clock.offset_hours()})
    return _after(db, req.hours, _run_passes(db))


def _related_ids(db: Session, subject_id: str) -> list[str]:
    """The debt's own id plus its promises' ids: a promise's reminder and
    check jobs are queued under the PROMISE id, not the debt's, so a per-debt
    view that ignores them says "nothing queued" while the check is waiting."""
    ids = [subject_id]
    ids += [p.id for p in db.query(PaymentPromise).filter(PaymentPromise.subject_id == subject_id).all()]
    return ids


@router.post("/clock/next")
def next_event(subject_id: str | None = None, db: Session = Depends(get_db)):
    """Jump to the next scheduled job (into the contact window if it is a
    customer-facing one) and run it. With `subject_id`, only that debt's next
    job counts - otherwise leftover debts from earlier scenarios keep
    capturing the jump."""
    q = db.query(ScheduledAction).filter(ScheduledAction.status == ScheduledStatus.SCHEDULED.value)
    if subject_id:
        q = q.filter(ScheduledAction.subject_id.in_(_related_ids(db, subject_id)))
    row = q.order_by(ScheduledAction.due_at.asc()).first()
    if row is None and subject_id:
        # An authorised autopay with nothing queued (its last debit finished or was refused): queue the next cycle debit
        # the way the customer's billing date would, so Next event always has something to walk.
        m = db.query(Mandate).filter(Mandate.id == subject_id, Mandate.status == "active").first()
        if m is not None:
            autopay.schedule_debit(db, m, clock.now() + CARD_TOKEN_DEBIT_LEAD)
            write_audit_entry(db, "demo_autopay_debit_queued", {"mandate_id": m.id, "via": "next_event"})
            row = q.order_by(ScheduledAction.due_at.asc()).first()
    if row is None:
        return _after(db, 0.0, {"executed": 0, "deferred": 0, "skipped": 0, "failed": 0},
                      note="Nothing is queued for this debt." if subject_id else "Nothing is scheduled.")
    target = max(row.due_at, clock.now())
    if row.kind in CONTACT_KINDS and not in_contact_window(target):
        target = next_contact_window_start(target)
    moved = clock.advance_to(target + timedelta(seconds=1))
    write_audit_entry(db, "demo_clock_advanced", {
        "to_next_job": row.kind, "hours": round(moved, 2), "offset_hours": clock.offset_hours()})
    return _after(db, moved, _run_passes(db), note=f"Jumped to the next job: {row.kind}")


@router.post("/clock/to-window")
def to_window(db: Session = Depends(get_db)):
    """Move to the next 08:00 IST if the clock is outside the contact window."""
    n = clock.now()
    if in_contact_window(n):
        return _after(db, 0.0, {"executed": 0, "deferred": 0, "skipped": 0, "failed": 0},
                      note="Already inside the contact window.")
    moved = clock.advance_to(next_contact_window_start(n) + timedelta(seconds=1))
    return _after(db, moved, _run_passes(db))


@router.post("/clock/reset")
def reset_clock(db: Session = Depends(get_db)):
    clock.reset()
    write_audit_entry(db, "demo_clock_reset", {})
    return {"clock": _clock_out()}


@router.post("/run-due")
def run_due(db: Session = Depends(get_db)):
    return _after(db, 0.0, _run_passes(db))


class VoiceModeIn(BaseModel):
    interactive: bool


@router.post("/voice-mode")
def voice_mode(req: VoiceModeIn):
    voice.set_interactive_mode(req.interactive)
    return {"interactive_calls": voice.interactive_mode()}


# ── Subjects (what can be demonstrated on) ───────────────────────────────────

def _open_promise(db: Session, st: str, sid: str) -> dict | None:
    p = db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == st, PaymentPromise.subject_id == sid,
        PaymentPromise.status == "open").first()
    return {"id": p.id, "promised_date": p.promised_date.isoformat(), "amount_inr": p.amount_inr} if p else None


def _promise_history(db: Session, st: str, sid: str) -> list[dict]:
    """Every promise on this debt, newest first, each with where it stands."""
    rows = (db.query(PaymentPromise)
            .filter(PaymentPromise.subject_type == st, PaymentPromise.subject_id == sid)
            .order_by(PaymentPromise.created_at.desc()).all())
    return [promises_mod._out(p) for p in rows]


def _mandate_contact(db: Session, m: Mandate):
    """Who we may contact for an authorised autopay: the shopper's own ContactPoint when they started it from their page,
    else the one linked to its Razorpay customer."""
    cp = contacts.for_customer(db, m.customer_id) if m.customer_id else None
    return cp or contacts.for_razorpay_customer(db, m.razorpay_customer_id)


def _mandate_title(m: Mandate) -> str:
    what = m.service_name or f"₹{m.amount_inr:,.0f} {m.frequency}"
    return f"Autopay · {what} · {m.customer_name}"


def _mandate_subject(db: Session, m: Mandate) -> dict:
    """An AUTHORISED autopay as a console item: its scheduled cycle debits are queued under the mandate's own id, so the
    queue and "Next event" work on it like on any debt. Nothing is owed yet - a failed debit later opens a failed-debit
    run, which is its own item (the queue offers a button to jump to it)."""
    nxt = (db.query(ScheduledAction).filter(ScheduledAction.subject_id == m.id,
                                            ScheduledAction.status == ScheduledStatus.SCHEDULED.value)
           .order_by(ScheduledAction.due_at.asc()).first())
    return {
        "subject_type": "mandate", "subject_id": m.id, "title": _mandate_title(m), "amount_inr": m.amount_inr,
        "status": m.status, "settled": m.status in ("cancelled", "rejected"),
        "contact": contacts.contact_out(_mandate_contact(db, m)),
        "self_serve_path": None, "open_promise": None, "promises": [],
        "next_action_at": nxt.due_at if nxt else None, "created_at": m.created_at,
    }


@router.get("/subjects")
def subjects(db: Session = Depends(get_db)):
    out = [_mandate_subject(db, m) for m in db.query(Mandate).order_by(Mandate.created_at.desc()).limit(15).all()]
    for r in db.query(MandateRun).order_by(MandateRun.opened_at.desc()).limit(15).all():
        out.append({
            "subject_type": "mandate_run", "subject_id": r.id,
            "title": f"Failed autopay debit · {r.subscription_id}", "amount_inr": r.amount_inr,
            "status": r.status, "settled": bool(r.risk_settled),
            "contact": contacts.contact_out(contacts.for_razorpay_customer(db, r.razorpay_customer_id)),
            "self_serve_path": link_path("mandate_run", r.id),
            "open_promise": _open_promise(db, "mandate_run", r.id),
            "promises": _promise_history(db, "mandate_run", r.id),
            "next_action_at": r.next_action_at, "created_at": r.opened_at,
        })
    for inv in db.query(Invoice).order_by(Invoice.created_at.desc()).limit(15).all():
        acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
        out.append({
            "subject_type": "invoice", "subject_id": inv.id,
            "title": f"Invoice {inv.invoice_no} · {acct.company_name if acct else ''}",
            "amount_inr": receivables._outstanding(inv), "status": inv.status,
            "settled": bool(inv.settled),
            "contact": contacts.contact_out(contacts.for_business_account(db, inv.account_id)),
            "self_serve_path": link_path("invoice", inv.id),
            "open_promise": _open_promise(db, "invoice", inv.id),
            "promises": _promise_history(db, "invoice", inv.id),
            "next_action_at": None, "created_at": inv.created_at,
        })
    # the test customers' debts always show, however many scenarios were created after them
    have = {r["subject_id"] for r in out}
    for row in db.query(demo_personas.DemoPersona).all():
        if row.subject_id in have:
            continue
        if row.subject_type == "invoice":
            inv = db.query(Invoice).filter(Invoice.id == row.subject_id).first()
            if inv is None:
                continue
            acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
            out.append({
                "subject_type": "invoice", "subject_id": inv.id,
                "title": f"Invoice {inv.invoice_no} · {acct.company_name if acct else ''}",
                "amount_inr": receivables._outstanding(inv), "status": inv.status, "settled": bool(inv.settled),
                "contact": contacts.contact_out(contacts.for_business_account(db, inv.account_id)),
                "self_serve_path": link_path("invoice", inv.id),
                "open_promise": _open_promise(db, "invoice", inv.id),
                "promises": _promise_history(db, "invoice", inv.id), "next_action_at": None,
                "created_at": inv.created_at,
            })
        else:
            r = db.query(MandateRun).filter(MandateRun.id == row.subject_id).first()
            if r is None:
                continue
            out.append({
                "subject_type": "mandate_run", "subject_id": r.id,
                "title": f"Failed autopay debit · {r.subscription_id}", "amount_inr": r.amount_inr,
                "status": r.status, "settled": bool(r.risk_settled),
                "contact": contacts.contact_out(contacts.for_razorpay_customer(db, r.razorpay_customer_id)),
                "self_serve_path": link_path("mandate_run", r.id),
                "open_promise": _open_promise(db, "mandate_run", r.id),
                "promises": _promise_history(db, "mandate_run", r.id),
                "next_action_at": r.next_action_at, "created_at": r.opened_at,
            })
    # Failed storefront orders: a real link + at most one reminder, no promise.
    from app.models import Customer
    for o in db.query(Order).filter(Order.status == OrderStatus.FAILED).order_by(Order.created_at.desc()).limit(10).all():
        cust = db.query(Customer).filter(Customer.id == o.customer_id).first()
        out.append({
            "subject_type": "order", "subject_id": o.id,
            "title": f"Failed order · ₹{o.amount_inr:,.0f} · {cust.name if cust else ''}",
            "amount_inr": o.amount_inr, "status": o.status.value,
            "settled": bool(o.risk_settled),
            "contact": contacts.contact_out(contacts.for_customer(db, o.customer_id)),
            "self_serve_path": None, "open_promise": None, "promises": [],
            "next_action_at": None, "created_at": o.created_at,
        })
    out.sort(key=lambda r: r["created_at"] or datetime.min, reverse=True)
    return out


_TEMPLATE_LABEL = {
    "otp": "verification code", "pre_debit_notice": "retry notice", "payment_link": "payment link",
    "remandate_link": "set up autopay again", "promise_reminder": "promise reminder",
    "invoice_courtesy": "invoice reminder", "invoice_overdue": "overdue notice",
}


@router.get("/timeline")
def timeline(subject_type: str, subject_id: str, db: Session = Depends(get_db)):
    """Everything that happened (and is queued) for one debt, oldest first,
    in plain sentences (app/demo_text.py). `raw` keeps the system's own label."""
    items: list[dict] = []
    for m in db.query(OutboundMessage).filter(
            OutboundMessage.subject_type == subject_type, OutboundMessage.subject_id == subject_id).all():
        d = demo_text.describe_message(m.template, _TEMPLATE_LABEL.get(m.template, m.template), m.status)
        items.append({"at": m.created_at, "type": "message", **d, "body": m.body,
                      "raw": f"{m.template} / {m.status}"})
    for c in db.query(VoiceCall).filter(
            VoiceCall.subject_type == subject_type, VoiceCall.subject_id == subject_id).all():
        d = demo_text.describe_call(c.status, c.routed_to, c.transcript)
        items.append({"at": c.created_at, "type": "call", **d, "body": c.transcript or "",
                      "raw": f"call {c.status} / {c.intent or '-'} -> {c.routed_to or '-'}"})
    for p in db.query(PaymentPromise).filter(
            PaymentPromise.subject_type == subject_type, PaymentPromise.subject_id == subject_id).all():
        st = promises_mod.promise_state(p)
        d = demo_text.describe_promise(p.promised_date, p.status)
        d["detail"] = st["label"]
        d["tone"] = {"kept": "ok", "partly_kept": "warn", "broken": "bad", "in_grace": "warn",
                     "due_today": "warn"}.get(st["state"], "info")
        items.append({"at": p.created_at, "type": "promise", **d, "body": "",
                      "raw": f"promise {p.status} / via {p.captured_via}"})
    for a in db.query(ScheduledAction).filter(
            ScheduledAction.subject_id.in_(_related_ids(db, subject_id))).all():
        # A finished notice/reminder already shows as its own text message, and
        # a call that rang shows as its own call item - don't say it twice.
        if a.status == "done" and (
                a.kind in ("pre_debit_notice", "promise_reminder")
                or (a.kind == "voice_call" and (a.result or "").startswith(
                    ("voice_call_placed", "voice_call_ringing")))):
            continue
        d = demo_text.describe_job(a.kind, a.status, a.result, a.due_at, a.executed_at)
        items.append({"type": "job", **d, "body": "", "raw": f"{a.kind} / {a.status} / {a.result or '-'}"})
    items.sort(key=lambda r: r["at"] or datetime.min)
    return items


# ── Money ────────────────────────────────────────────────────────────────────

_FIELD_LABEL = {
    "at_risk_mandate": "At risk - failed autopay debits",
    "total_recovered": "Recovered",
    "total_lost": "Lost",
    "receivables_overdue": "Overdue invoices",
    "receivables_collected": "Collected invoices",
    "receivables_written_off": "Written off",
    "total_revenue": "Revenue",
}
_REASON_PLAIN = {
    "mandate_run_opened": "the debit failed, so the amount became at risk",
    "mandate_subscription_charged": "the debit finally succeeded",
    "mandate_lapsed_no_recovery": "nobody recovered it before the deadline, so it was given up",
    "invoice_overdue": "the due date passed unpaid",
    "invoice_paid": "the invoice was paid",
    "invoice_written_off": "a person wrote the invoice off",
    "invoice_credit_note": "a credit note reduced what is owed",
}


def _money_trail(db: Session, ref: str) -> list[dict]:
    """The ledger entries that mention this debt (subscription id / invoice
    no), oldest first - straight from the audited ledger."""
    rows = (db.query(AuditLog).filter(AuditLog.action_type == "revenue_state_updated",
                                      AuditLog.details.contains(ref))
            .order_by(AuditLog.sequence_num.asc()).all())
    out = []
    for r in rows:
        try:
            d = json.loads(r.details)
        except ValueError:
            continue
        if d.get("order_id") != ref:
            continue
        reason = str(d.get("reason", ""))
        out.append({
            "at": r.created_at, "field": d.get("field"),
            "field_label": _FIELD_LABEL.get(d.get("field"), str(d.get("field"))),
            "delta": d.get("delta"), "before": d.get("before"), "after": d.get("after"),
            "why": _REASON_PLAIN.get(reason, reason.replace("_", " ")),
        })
    return out


def _mandate_money(db: Session, run: MandateRun) -> dict:
    n = clock.now()
    broken = db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == "mandate_run", PaymentPromise.subject_id == run.id,
        PaymentPromise.status == "broken").first() is not None
    base = {"amount_inr": run.amount_inr, "ref": run.subscription_id, "lapse_at": None,
            "hours_to_lapse": None}
    if run.risk_settled:
        if run.status == "recovered":
            return {**base, "state": "recovered", "tone": "ok",
                    "headline": f"Recovered: ₹{run.amount_inr:,.0f} moved out of \"At risk\" into \"Recovered\".",
                    "detail": "The payment arrived, so the money left the at-risk bucket exactly once."}
        return {**base, "state": "lost", "tone": "bad",
                "headline": f"Lost: ₹{run.amount_inr:,.0f} moved out of \"At risk\" into \"Lost\".",
                "detail": "Nobody recovered it before the deadline, so it was given up."}
    lapse = mandate.lapse_due_at(run)
    hrs = max(0.0, (lapse - n).total_seconds() / 3600)
    why = {
        "active": "Retries are still planned. It stays at risk until a debit succeeds or the plan runs out.",
        "exhausted": "Automatic recovery has stopped (retries used up, a broken promise or a dispute). "
                     "A person can still rescue it.",
        "halted": "The mandate was cancelled; the customer has to set autopay up again.",
    }.get(run.status, "")
    detail = why
    if broken:
        detail += (" The broken promise itself moved no money: a promise only pauses chasing, "
                   "money moves only on a real payment or when the debt is given up.")
    detail += f" If it is not recovered it moves to \"Lost\" on {demo_text._ist(lapse)} IST."
    return {**base, "state": "at_risk", "tone": "warn",
            "headline": f"Still at risk: ₹{run.amount_inr:,.0f} is counted under "
                        f"\"At risk - failed autopay debits\".",
            "detail": detail.strip(), "lapse_at": lapse, "hours_to_lapse": round(hrs, 1)}


def _invoice_money(db: Session, inv: Invoice) -> dict:
    out = receivables._outstanding(inv)
    base = {"amount_inr": inv.amount_inr, "ref": inv.invoice_no, "lapse_at": None, "hours_to_lapse": None}
    if inv.status == "paid":
        return {**base, "state": "recovered", "tone": "ok",
                "headline": f"Collected: ₹{inv.amount_paid:,.0f} moved from \"Overdue invoices\" to \"Collected\".",
                "detail": "Paid in full."}
    if inv.status == "written_off":
        return {**base, "state": "lost", "tone": "bad",
                "headline": "Written off: moved out of \"Overdue invoices\" into \"Written off\".",
                "detail": "A person decided to give this invoice up."}
    if not inv.overdue_booked:
        return {**base, "state": "not_due", "tone": "info",
                "headline": f"₹{out:,.0f} is not at risk yet.",
                "detail": f"It becomes \"Overdue\" when the due date ({demo_text._ist(inv.due_at, False)}) passes unpaid."}
    paid = f" ₹{inv.amount_paid:,.0f} has already been collected." if inv.amount_paid else ""
    return {**base, "state": "at_risk", "tone": "warn",
            "headline": f"Overdue: ₹{out:,.0f} is counted under \"Overdue invoices\".",
            "detail": ("It leaves that bucket only when it is paid or a person writes it off. "
                       "Business invoices are never written off automatically." + paid)}


@router.get("/money")
def money(subject_type: str | None = None, subject_id: str | None = None, db: Session = Depends(get_db)):
    """The merchant ledger, plus (for one debt) where its money sits right
    now and the audited entries that moved it."""
    state = revenue.get_or_create_state(db)
    out: dict = {"ledger": revenue.as_dict(state), "subject": None, "trail": []}
    if subject_type == "mandate_run" and subject_id:
        run = db.query(MandateRun).filter(MandateRun.id == subject_id).first()
        if run:
            out["subject"] = _mandate_money(db, run)
            out["trail"] = _money_trail(db, run.subscription_id)
    elif subject_type == "invoice" and subject_id:
        inv = db.query(Invoice).filter(Invoice.id == subject_id).first()
        if inv:
            out["subject"] = _invoice_money(db, inv)
            out["trail"] = _money_trail(db, inv.invoice_no)
    return out


# ── Scenarios ────────────────────────────────────────────────────────────────

class MandateScenarioIn(BaseModel):
    phone: str = "9876543210"
    language: str = "hinglish"
    amount_inr: float = 499.0
    reason: str = "insufficient_funds"   # insufficient_funds | bank_decline | mandate_revoked


_REASONS = {
    "insufficient_funds": ("BAD_REQUEST_ERROR", "insufficient funds in account"),
    "bank_decline": ("GATEWAY_ERROR", "transaction declined by issuing bank"),
    "mandate_revoked": ("BAD_REQUEST_ERROR", "mandate cancelled by customer"),
}


@router.post("/scenarios/mandate")
def scenario_mandate(req: MandateScenarioIn, db: Session = Depends(get_db)):
    """A failed recurring debit, created the way a subscription.pending webhook
    would. The customer has a merchant-entered number and NOTHING else - they
    must verify it and consent on their own page."""
    from app.webhook import _classify_failure_reason, _extract_error_fields
    if req.reason not in _REASONS:
        raise HTTPException(422, f"reason must be one of {sorted(_REASONS)}")
    try:
        phone = normalize_phone(req.phone)
    except OtpError as e:
        raise HTTPException(422, detail={"code": e.code, "message": e.message})
    tag = uuid.uuid4().hex[:8]
    cust, sub = f"cust_demo_{tag}", f"sub_demo_{tag}"
    contacts.upsert_contact(db, razorpay_customer_id=cust, phone=phone, language=req.language)

    code, desc = _REASONS[req.reason]
    entity = {"id": f"pay_demo_{tag}", "amount": int(round(req.amount_inr * 100)),
              "customer_id": cust, "subscription_id": sub, "error_code": code, "error_reason": desc}
    payload = {"event": "subscription.pending", "payload": {
        "payment": {"entity": entity}, "subscription": {"entity": {"id": sub, "payment_attempts": 1}}}}
    event = Event(
        razorpay_event_id=f"demo:{tag}", event_type=EventType.SUBSCRIPTION_PENDING,
        subscription_id=sub, payment_id=entity["id"], customer_id=cust, amount_inr=req.amount_inr,
        failure_reason=_classify_failure_reason(entity), attempt_count=1,
        raw_payload=json.dumps(payload), **_extract_error_fields(entity))
    db.add(event)
    db.commit()
    db.refresh(event)
    write_audit_entry(db, "event_received", {
        "event_type": event.event_type.value, "failure_reason": event.failure_reason.value,
        "attempt_count": 1, "amount_inr": req.amount_inr, "demo_scenario": True}, event_id=event.id)
    mandate.process_mandate_event(db, event)
    run = db.query(MandateRun).filter(MandateRun.subscription_id == sub).first()
    return {"subject_type": "mandate_run", "subject_id": run.id if run else None,
            "subscription_id": sub, "self_serve_path": link_path("mandate_run", run.id) if run else None}


class AutopayScenarioIn(BaseModel):
    name: str = "Demo Autopay Customer"
    phone: str = "9876543210"
    email: str | None = None   # Razorpay requires one; a unique demo address is used if blank
    amount_inr: float = 499.0
    frequency: str = "monthly"


@router.post("/scenarios/autopay")
def scenario_autopay(req: AutopayScenarioIn, db: Session = Depends(get_db)):
    """A REAL card mandate on Razorpay Test Mode (app/autopay.py): returns the
    Razorpay-hosted page where you authorise it with a test card. Unlike the
    other scenarios nothing is simulated - the debits are real Test Mode
    debits that our scheduler triggers. Pick the card to choose the outcome:
    4100 2800 0000 1007 (later debits succeed) or 4718 6000 0007 0021 (later
    debits fail: insufficient funds)."""
    from app import autopay, razorpay_client as rz
    try:
        phone = normalize_phone(req.phone)
    except OtpError as e:
        raise HTTPException(422, detail={"code": e.code, "message": e.message})
    if not rz.keys_present():
        raise HTTPException(409, "Set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (Test Mode) first.")
    try:
        email = req.email or f"demo.autopay+{uuid.uuid4().hex[:8]}@example.com"
        m = autopay.create_mandate(db, name=req.name, phone=phone, email=email, amount_inr=req.amount_inr,
                                   frequency=req.frequency)
    except rz.RazorpayError as e:
        raise HTTPException(502, f"Razorpay refused: {e.code} {e.description}")
    return {"mandate_id": m.id, "registration_url": m.registration_url, "status": m.status,
            "test_cards": {"debits_succeed": "4100 2800 0000 1007",
                           "debits_fail_insufficient_funds": "4718 6000 0007 0021"}}


class InvoiceScenarioIn(BaseModel):
    company_name: str = "Sharma Traders"
    phone: str = "9876500001"
    language: str = "hinglish"
    amount_inr: float = 45000.0
    days_overdue: int = 8


@router.post("/scenarios/invoice")
def scenario_invoice(req: InvoiceScenarioIn, db: Session = Depends(get_db)):
    try:
        phone = normalize_phone(req.phone)
    except OtpError as e:
        raise HTTPException(422, detail={"code": e.code, "message": e.message})
    if req.language not in ("en", "hi", "hinglish"):
        raise HTTPException(422, "language must be one of en, hi, hinglish")
    tag = uuid.uuid4().hex[:6].upper()
    acct = BusinessAccount(company_name=f"{req.company_name} ({tag})", contact_name="Accounts",
                           phone=phone, preferred_language=req.language, voice_consent=False)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    contacts.upsert_contact(db, business_account_id=acct.id, phone=phone, language=req.language)
    write_audit_entry(db, "business_account_created", {
        "account_id": acct.id, "company_name": acct.company_name, "demo_scenario": True})
    now = clock.now()
    inv = receivables.create_invoice(
        db, acct, f"DEMO-{tag}", req.amount_inr,
        issued_at=now - timedelta(days=req.days_overdue + 30),
        due_at=now - timedelta(days=req.days_overdue))
    return {"subject_type": "invoice", "subject_id": inv.id, "invoice_no": inv.invoice_no,
            "self_serve_path": link_path("invoice", inv.id)}


_STEP_NAMES = ["Courtesy reminder", "Payment-link message", "Payment link again (text message)", "Voice call"]


def _ladder_steps(db: Session, inv: Invoice) -> dict:
    """The invoice's chase ladder as one readable list: what ran, what is
    queued, and - for the call step - what WILL happen given today's state."""
    from app.rules_engine import ESCALATE_STEP
    rows = (db.query(ScheduledAction)
            .filter(ScheduledAction.subject_type == "invoice", ScheduledAction.subject_id == inv.id,
                    ScheduledAction.kind == "invoice_chase")
            .order_by(ScheduledAction.due_at.asc(), ScheduledAction.created_at.asc()).all())
    latest: dict[int, ScheduledAction] = {}
    for r in rows:   # a resumed ladder re-queues a step; the newest row is the truth
        try:
            st = int(json.loads(r.payload_json).get("step"))
        except (TypeError, ValueError):
            continue
        latest[st] = r
    threshold = float(runtime_flags.get_high_value_amount_inr())
    out_amt = receivables._outstanding(inv)
    ok, why = contacts.can_call(contacts.for_business_account(db, inv.account_id))
    steps = []
    for st in sorted(latest):
        r = latest[st]
        d = demo_text.describe_job(r.kind, r.status, r.result, r.due_at, r.executed_at)
        label = "Hand to finance (a person takes over)" if st == ESCALATE_STEP else _STEP_NAMES[st]
        hint = ""
        if r.status == "done" and st == 3 and out_amt >= threshold:
            d = {**d, "title": "A person is asked to call",
                 "detail": f"₹{out_amt:,.0f} is at or above the ₹{threshold:,.0f} high-value limit, "
                           "so no automated call was made."}
        if r.status == "scheduled" and st == 3:
            if out_amt >= threshold:
                hint = (f"Will NOT call: ₹{out_amt:,.0f} is at or above the ₹{threshold:,.0f} "
                        "high-value limit, so a person is asked to call instead.")
            elif not ok:
                hint = ("Will fall back to a payment-link message: the customer has not "
                        f"{'verified the number' if why == 'phone_not_verified' else 'ticked call consent'} yet.")
            else:
                hint = "Will place the call (number verified, consent given)."
        steps.append({"step": st, "label": label, "status": r.status, "due": demo_text._ist(r.due_at),
                      "title": d["title"], "detail": d["detail"], "tone": d["tone"], "hint": hint})
    return {"invoice": {"invoice_no": inv.invoice_no, "status": inv.status, "settled": bool(inv.settled),
                        "outstanding_inr": round(out_amt, 2),
                        "due": demo_text._ist(inv.due_at, False)},
            "steps": steps}


@router.get("/invoice-ladder")
def invoice_ladder(subject_id: str, db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == subject_id).first()
    if inv is None:
        raise HTTPException(404, "Unknown invoice")
    return _ladder_steps(db, inv)


@router.post("/invoice-autoplay")
def invoice_autoplay(subject_id: str, db: Session = Depends(get_db)):
    """Run the whole pipeline: keep jumping to this invoice's next job until
    nothing is queued (paid, paused by a promise, or handed to a person)."""
    inv = db.query(Invoice).filter(Invoice.id == subject_id).first()
    if inv is None:
        raise HTTPException(404, "Unknown invoice")
    fired = 0
    for _ in range(10):
        r = next_event(subject_id=subject_id, db=db)
        if r["ran"]["executed"] == 0 and r["ran"]["skipped"] == 0 and r["ran"]["failed"] == 0:
            break
        fired += r["ran"]["executed"] + r["ran"]["skipped"]
    db.expire_all()
    return {"fired": fired, **_ladder_steps(db, inv), "clock": _clock_out()}


class PaidIn(BaseModel):
    subject_type: str
    subject_id: str


@router.post("/payment")
def simulate_payment(req: PaidIn, db: Session = Depends(get_db)):
    """The customer pays. Goes through the same handlers a Razorpay webhook
    reaches (subscription.charged / payment-link paid), so a promise resolves
    and the ledger exits exactly once."""
    if req.subject_type == "mandate_run":
        run = db.query(MandateRun).filter(MandateRun.id == req.subject_id).first()
        if run is None:
            raise HTTPException(404, "Unknown mandate run")
        return mandate.handle_subscription_charged(
            db, {"subscription": {"entity": {"id": run.subscription_id}}})
    if req.subject_type == "invoice":
        inv = db.query(Invoice).filter(Invoice.id == req.subject_id).first()
        if inv is None:
            raise HTTPException(404, "Unknown invoice")
        return receivables.record_payment(
            db, inv.id, receivables._outstanding(inv), f"pay_demo_{uuid.uuid4().hex[:10]}")
    raise HTTPException(422, "subject_type must be mandate_run or invoice")


@router.post("/promise-day-paid")
def promise_day_paid(req: PaidIn, db: Session = Depends(get_db)):
    """The customer said "I will pay" and the day has come: jump the clock to
    the promised date (never backwards), the bank retry goes through and the
    payment arrives. Same handlers as a real retry + subscription.charged, so
    the promise resolves as kept and the money leaves at-risk exactly once."""
    open_p = (db.query(PaymentPromise)
              .filter(PaymentPromise.subject_type == req.subject_type,
                      PaymentPromise.subject_id == req.subject_id,
                      PaymentPromise.status == "open").first())
    if open_p is None:
        raise HTTPException(409, "There is no open promise on this debt. Record one first "
                                 "(on the call or the customer page).")
    target = datetime.combine(open_p.promised_date, datetime.min.time()) + timedelta(hours=5)  # ~10:30 IST
    if target > clock.now():
        clock.advance_to(target)
    if req.subject_type == "mandate_run":
        write_audit_entry(db, "mandate_retry_attempted", {
            "mandate_run_id": req.subject_id, "success": True,
            "outcome": "retry_succeeded_on_promised_day", "demo": True})
    out = simulate_payment(req, db)
    return {"promised_date": str(open_p.promised_date), "payment": out}


# ── Simulation Console: links switch, queue, finish-it buttons ───────────────
# The backend runs exactly as it would live. These routes only (a) choose
# whether the simulation may spend the ~30 real Test Mode links / ask Razorpay
# for a real debit, (b) show one debt's queue, and (c) drive that debt to its
# end with the normal "next event" loop while NOTHING real is spent.

MAX_FINISH_STEPS = 60
_REAL_RAZORPAY_KINDS = ("mandate_pdn", "mandate_debit")
_LINK_KINDS = ("invoice_chase", "promise_reminder", "pre_debit_notice", "order_link_reminder")


def _modes() -> dict:
    sim_links = runtime_flags.get_sim_real_links()
    outcome = runtime_flags.get_sim_debit_outcome()
    return {"real_links": sim_links is not False and payment_links.mode() == "real",
            "links_switch": sim_links,                       # None = not set from the console
            "debit": outcome or "real",
            "test_mode": rz.is_test_key(), "keys_present": rz.keys_present(),
            "window_days": settings.test_mode_window_days}


class LinksModeIn(BaseModel):
    real: bool


@router.get("/modes")
def modes(db: Session = Depends(get_db)):
    return {**_modes(), "links": payment_links.usage(db, include_account=True)}


@router.post("/links-mode")
def set_links_mode(req: LinksModeIn, db: Session = Depends(get_db)):
    runtime_flags.set_sim_real_links(bool(req.real))
    write_audit_entry(db, "demo_links_mode_set", {"real_links": req.real})
    return modes(db)


class DebitModeIn(BaseModel):
    outcome: str    # real | success | insufficient_funds


@router.post("/debit-mode")
def set_debit_mode(req: DebitModeIn, db: Session = Depends(get_db)):
    if req.outcome not in ("real", "success", "insufficient_funds"):
        raise HTTPException(422, "outcome must be real, success or insufficient_funds")
    runtime_flags.set_sim_debit_outcome(None if req.outcome == "real" else req.outcome)
    write_audit_entry(db, "demo_debit_mode_set", {"outcome": req.outcome})
    return modes(db)


def _subject_summary(db: Session, st: str, sid: str) -> dict:
    try:
        uuid.UUID(str(sid))
    except ValueError:
        raise HTTPException(404, "Unknown debt")
    if st == "invoice":
        inv = db.query(Invoice).filter(Invoice.id == sid).first()
        if inv is None:
            raise HTTPException(404, "Unknown invoice")
        acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
        return {"title": f"Invoice {inv.invoice_no} · {acct.company_name if acct else ''}",
                "status": inv.status, "settled": bool(inv.settled), "amount_inr": receivables._outstanding(inv)}
    if st == "mandate":
        m = db.query(Mandate).filter(Mandate.id == sid).first()
        if m is None:
            raise HTTPException(404, "Unknown autopay")
        return {"title": _mandate_title(m), "status": m.status, "settled": m.status in ("cancelled", "rejected"),
                "amount_inr": m.amount_inr}
    if st == "mandate_run":
        run = db.query(MandateRun).filter(MandateRun.id == sid).first()
        if run is None:
            raise HTTPException(404, "Unknown mandate run")
        return {"title": f"Failed autopay debit · {run.subscription_id}", "status": run.status,
                "settled": bool(run.risk_settled), "amount_inr": run.amount_inr}
    if st == "order":
        o = db.query(Order).filter(Order.id == sid).first()
        if o is None:
            raise HTTPException(404, "Unknown order")
        return {"title": f"Failed order · ₹{o.amount_inr:,.0f}", "status": o.status.value,
                "settled": o.status == OrderStatus.CAPTURED or bool(o.risk_settled), "amount_inr": o.amount_inr}
    raise HTTPException(422, "subject_type must be invoice, mandate, mandate_run or order")


def _queued_rows(db: Session, sid: str) -> list[ScheduledAction]:
    return (db.query(ScheduledAction)
            .filter(ScheduledAction.status == ScheduledStatus.SCHEDULED.value,
                    ScheduledAction.subject_id.in_(_related_ids(db, sid)))
            .order_by(ScheduledAction.due_at.asc()).all())


@router.get("/queue")
def queue(subject_type: str, subject_id: str, db: Session = Depends(get_db)):
    """One debt's confirmed next steps, soonest first - what "Next event" will
    walk through. Each item says when it would actually run (a customer-facing
    one waits for 08:00 IST) and, if it needs Razorpay, whether that is a real
    call or a played result."""
    summary = _subject_summary(db, subject_type, subject_id)
    now = clock.now()
    m = _modes()
    hints: dict[int, str] = {}
    labels: dict[int, str] = {}
    if subject_type == "invoice":
        inv = db.query(Invoice).filter(Invoice.id == subject_id).first()
        ladder = _ladder_steps(db, inv)["steps"]
        hints = {s["step"]: s["hint"] for s in ladder if s["hint"]}
        labels = {s["step"]: s["label"] for s in ladder}
    items = []
    for r in _queued_rows(db, subject_id):
        runs_at = r.due_at
        if r.kind in CONTACT_KINDS and not in_contact_window(runs_at):
            runs_at = next_contact_window_start(runs_at)
        runs_at = max(runs_at, now)
        days = (runs_at - now).total_seconds() / 86400
        d = demo_text.describe_job(r.kind, r.status, r.result, r.due_at, r.executed_at)
        try:
            step = int(json.loads(r.payload_json or "{}").get("step"))
        except (TypeError, ValueError):
            step = None
        if step is not None and step in labels and r.status == ScheduledStatus.SCHEDULED.value:
            d = {**d, "title": f"Planned: {labels[step]}"}
        real_rz = r.kind in _REAL_RAZORPAY_KINDS
        warn = None
        try:
            ready = datetime.fromisoformat(json.loads(r.payload_json or "{}").get("real_debit_at") or "")
        except (TypeError, ValueError):
            ready = None
        if r.kind == ScheduledKind.MANDATE_DEBIT.value and ready is not None and m["debit"] == "real" and ready > datetime.utcnow():
            warn = (f"Razorpay's test environment runs on real time and only accepts this debit from "
                    f"{demo_text._ist(ready)} IST (36 hours after its pre-debit notice). Pressing Next event earlier is refused "
                    f"and the step waits. To simulate it now, choose “Play: debit succeeds” or “Play: debit fails” in the settings below.")
        if warn is None and real_rz and m["debit"] == "real" and days > m["window_days"]:
            warn = (f"This step calls Razorpay for real and is {days:.1f} days away. Razorpay's test environment "
                    f"runs on real time, and its docs give no guarantee past a few days - keep real steps within "
                    f"about {m['window_days']} days, or choose a played result below.")
        items.append({
            "id": r.id, "kind": r.kind, "plain": demo_text.KIND_PLAIN.get(r.kind, r.kind.replace("_", " ")),
            "title": d["title"], "detail": d["detail"], "due_at": r.due_at, "runs_at": runs_at,
            "runs_at_ist": demo_text._ist(runs_at), "days_ahead": round(days, 2),
            "customer_facing": r.kind in CONTACT_KINDS, "uses_real_razorpay": real_rz and m["debit"] == "real",
            "played_result": real_rz and m["debit"] != "real", "may_carry_link": r.kind in _LINK_KINDS,
            "hint": hints.get(step, "") if step is not None else "", "warn": warn,
        })
    follow_up = None
    outlook: list[dict] = []
    if subject_type == "mandate":
        mm = db.query(Mandate).filter(Mandate.id == subject_id).first()
        if mm is not None and mm.status == "active":
            every = {"weekly": 7, "monthly": 30, "yearly": 365}.get(mm.frequency, 30)
            outlook = [
                {"when": "If the debit succeeds", "tone": "ok",
                 "text": f"₹{mm.amount_inr:,.0f} is booked as revenue once, and the next {mm.frequency} debit is queued by itself "
                         f"(about {every} days later). It shows here as soon as the result arrives."},
                {"when": "If the debit fails", "tone": "bad",
                 "text": "A failed-debit item opens and the money counts as “at risk”. Its steps are queued then: a retry at least 25 hours "
                         "later with a notice 24 hours before, a call about 18 hours before the retry, and a promise pauses it. "
                         f"After {runtime_flags.get_mandate_max_retries()} retries or {runtime_flags.get_mandate_retry_window_days()} days "
                         "it is given up as lost. This banner then offers to open that item."},
                {"when": "Why these are not listed above", "tone": "info",
                 "text": "A step that depends on the debit's result can only be queued once the result exists. Only the notice and the debit are known in advance."},
            ]
    if subject_type == "mandate":
        # a failed debit opened a run for this autopay: that is where the recovery now happens
        run = (db.query(MandateRun).filter(MandateRun.mandate_id == subject_id)
               .order_by(MandateRun.opened_at.desc()).first())
        if run is not None:
            follow_up = {"subject_type": "mandate_run", "subject_id": run.id,
                         "title": f"Failed autopay debit · {run.subscription_id}", "status": run.status,
                         "settled": bool(run.risk_settled), "amount_inr": run.amount_inr}
    return {"subject": {"subject_type": subject_type, "subject_id": subject_id, **summary,
                        "open_promise": _open_promise(db, subject_type, subject_id)},
            "items": items, "clock": _clock_out(), "modes": m, "follow_up": follow_up, "outlook": outlook}


class DriveIn(BaseModel):
    subject_type: str
    subject_id: str


@contextmanager
def _customer_ignores_us():
    """For the run-to-the-end buttons: no real link, no real debit, and the
    customer never picks up (so nobody has to answer a ringing browser)."""
    prev_i, prev_s = voice.interactive_mode(), voice._sim["scenario"]
    voice.set_interactive_mode(False)
    voice.set_simulated_scenario("no_answer")
    try:
        with payment_links.force_stub(), autopay.simulate_debits("insufficient_funds"):
            yield
    finally:
        voice.set_interactive_mode(prev_i)
        voice.set_simulated_scenario(prev_s)


def _drive(db: Session, req: DriveIn, until_promise_resolved: bool) -> dict:
    summary = _subject_summary(db, req.subject_type, req.subject_id)
    taken: list[dict] = []
    with _customer_ignores_us():
        for _ in range(MAX_FINISH_STEPS):
            if until_promise_resolved and _open_promise(db, req.subject_type, req.subject_id) is None:
                break
            rows = _queued_rows(db, req.subject_id)
            if not rows:
                break
            row = rows[0]
            next_event(subject_id=req.subject_id, db=db)
            db.expire_all()
            row = db.query(ScheduledAction).filter(ScheduledAction.id == row.id).first()
            if row is not None and row.status != ScheduledStatus.SCHEDULED.value:
                d = demo_text.describe_job(row.kind, row.status, row.result, row.due_at, row.executed_at)
                taken.append({"title": d["title"], "detail": d["detail"], "tone": d["tone"],
                              "at": demo_text._ist(row.executed_at or row.due_at)})
        if not until_promise_resolved and req.subject_type == "mandate_run":
            run = db.query(MandateRun).filter(MandateRun.id == req.subject_id).first()
            if run is not None and not run.risk_settled and not _queued_rows(db, req.subject_id):
                # Nothing left to try: the money leaves "at risk" when the sweep
                # gives the run up at its deadline - in real life a timer, here the clock.
                clock.advance_to(mandate.lapse_due_at(run) + timedelta(hours=1))
                _run_passes(db)
                db.expire_all()
    final = _subject_summary(db, req.subject_type, req.subject_id)
    write_audit_entry(db, "demo_finish_unpaid" if not until_promise_resolved else "demo_promise_broken_run",
                      {"subject_type": req.subject_type, "subject_id": req.subject_id, "steps": len(taken),
                       "final_status": final["status"]})
    return {"steps": taken, "before": summary, "after": final, "clock": _clock_out(),
            "stopped_at_cap": len(taken) >= MAX_FINISH_STEPS,
            "real_links_spent": 0, "real_debits_made": 0}


@router.post("/finish-unpaid")
def finish_unpaid(req: DriveIn, db: Session = Depends(get_db)):
    """The customer never pays: keep pressing "Next event" for this debt until
    nothing is queued, with every link a labelled stand-in, every debit a played
    failure and every call unanswered. The real rules decide each step."""
    return _drive(db, req, until_promise_resolved=False)


@router.post("/promise-broken")
def promise_broken(req: DriveIn, db: Session = Depends(get_db)):
    """The customer promised a date and did not pay: walk this debt until the
    promise resolves (it is judged broken after its grace day)."""
    if _open_promise(db, req.subject_type, req.subject_id) is None:
        raise HTTPException(409, "There is no open promise on this debt.")
    return _drive(db, req, until_promise_resolved=True)


@router.post("/check-link")
def check_link(req: DriveIn, db: Session = Depends(get_db)):
    """"Check payment now": ask Razorpay about this debt's open Payment Link and
    apply the result (the same thing the webhook does), for setups with no tunnel."""
    link = (db.query(PaymentLink).filter(PaymentLink.subject_id == req.subject_id)
            .order_by(PaymentLink.created_at.desc()).first())
    if link is None:
        return {"checked": False, "note": "No payment link has been made for this debt yet."}
    return {**payment_links.sync_link(db, link), "short_url": link.short_url, "is_stub": link.is_stub}


# ── Interactive calls ────────────────────────────────────────────────────────

class CallStartIn(BaseModel):
    subject_type: str
    subject_id: str


@router.post("/calls/start")
def call_start(req: CallStartIn, db: Session = Depends(get_db)):
    return voice.start_interactive_call(db, req.subject_type, req.subject_id)


class CallReplyIn(BaseModel):
    transcript: str = ""


@router.post("/calls/{call_id}/reply")
def call_reply(call_id: str, req: CallReplyIn, db: Session = Depends(get_db)):
    out = voice.finish_interactive_call(db, call_id, req.transcript)
    if not out.get("ok"):
        raise HTTPException(409, out.get("error", "call_failed"))
    text, tone = demo_text.outcome_text(out.get("routed_to"))
    if (out.get("routed_to") or "").endswith("date_too_far"):
        text += (f" Promises can be at most {runtime_flags.get_promise_max_days_b2c()} days ahead "
                 f"for consumers ({runtime_flags.get_promise_max_days_b2b()} for businesses).")
    return {**out, "explanation": text, "tone": tone}


@router.get("/memory")
def memory_view(subject_type: str, subject_id: str, db: Session = Depends(get_db)):
    """What the assistant would remember about this debt's customer."""
    approach = None
    if subject_type == "mandate":        # an authorised autopay owes nothing yet: no debt records, but the person's memory is theirs
        m = db.query(Mandate).filter(Mandate.id == subject_id).first()
        if m is None:
            raise HTTPException(404, "Unknown autopay")
        cp, records = _mandate_contact(db, m), []
    else:
        adapter = voice._ADAPTERS.get(subject_type)
        target = adapter(db, subject_id, clock.now()) if adapter else None
        if target is None:
            raise HTTPException(404, "Unknown subject")
        cp, records = target.contact, memory_mod.structured_context(db, None, target)
        approach = customer_profile.build(db, target)
    return {
        "approach": ({"name": approach.approach, "why": approach.why, "pay_day": approach.pay_day, "call_slot": approach.call_slot,
                      "facts_used": approach.consented} if subject_type != "mandate" else None),
        "contact_id": cp.id if cp else None,
        "memory_consent": bool(cp and cp.memory_consent),
        "records": records,
        "notes": memory_mod.notes_for_contact(db, cp.id) if cp else [],
        "retention_days": settings.memory_retention_days,
        "memory_consent_current": contacts.has_full_memory_consent(cp),
        "tiers": _tier_counts(db, cp.id) if cp else {},
    }


def _tier_counts(db: Session, contact_id: str) -> dict:
    from app.rag_models import ConversationEpisode, CustomerFact, CustomerMemory, EpisodeChunk, MemoryLink
    return {"notes": db.query(CustomerMemory).filter(CustomerMemory.contact_id == contact_id).count(),
            "episodes": db.query(ConversationEpisode).filter(ConversationEpisode.contact_id == contact_id).count(),
            "chunks": db.query(EpisodeChunk).filter(EpisodeChunk.contact_id == contact_id).count(),
            "facts": db.query(CustomerFact).filter(CustomerFact.contact_id == contact_id, CustomerFact.status == "active").count(),
            "links": db.query(MemoryLink).filter(MemoryLink.contact_id == contact_id).count()}


def _contact_for(db: Session, subject_type: str, subject_id: str):
    adapter = voice._ADAPTERS.get(subject_type)
    target = adapter(db, subject_id, clock.now()) if adapter else None
    if target is None:
        raise HTTPException(404, "Unknown subject")
    return target


@router.get("/memory/explorer")
def memory_explorer(subject_type: str, subject_id: str, db: Session = Depends(get_db)):
    """Everything kept about this customer, tier by tier (the Demo Console's Memory explorer)."""
    from app.rag_models import ConversationEpisode, CustomerFact, EpisodeChunk, MemoryLink
    target = _contact_for(db, subject_type, subject_id)
    cp = target.contact
    if cp is None:
        return {"contact_id": None}
    core, _ = memory_retrieval.core_card(db, cp.id, clock.now())
    episodes = (db.query(ConversationEpisode).filter(ConversationEpisode.contact_id == cp.id)
                .order_by(ConversationEpisode.started_at.desc()).all())
    chunks: dict[str, list] = {}
    for c in (db.query(EpisodeChunk).filter(EpisodeChunk.contact_id == cp.id)
              .order_by(EpisodeChunk.episode_id, EpisodeChunk.seq).all()):
        chunks.setdefault(c.episode_id, []).append({"id": c.id, "seq": c.seq, "turn_from": c.turn_from,
                                                    "turn_to": c.turn_to, "text": c.text,
                                                    "embedded": c.embedding is not None})
    facts = (db.query(CustomerFact).filter(CustomerFact.contact_id == cp.id)
             .order_by(CustomerFact.key, CustomerFact.valid_from.desc()).all())
    links = db.query(MemoryLink).filter(MemoryLink.contact_id == cp.id).order_by(MemoryLink.created_at).all()
    # the agent turns of this customer's calls that read memory, newest first (what the last answers drew on)
    call_ids = [c.id for c in db.query(VoiceCall).filter(VoiceCall.customer_ref == cp.id).all()]
    turns = []
    if call_ids:
        for t in (db.query(VoiceTurn).filter(VoiceTurn.call_id.in_(call_ids), VoiceTurn.speaker == "agent",
                                              VoiceTurn.timings_json.isnot(None))
                  .order_by(VoiceTurn.created_at.desc()).limit(40).all()):
            tm = json.loads(t.timings_json)
            if tm.get("memory"):
                turns.append({"call_id": t.call_id, "turn_no": t.turn_no, "kind": t.kind, "text": t.text,
                              "sources": json.loads(t.sources) if t.sources else None, "memory": tm["memory"]})
            if len(turns) >= 8:
                break
    # working memory: the turns of this customer's most recent call (it may still be in progress)
    working, last_call, wt = None, None, []
    for cand in (db.query(VoiceCall).filter(VoiceCall.customer_ref == cp.id)
                 .order_by(VoiceCall.created_at.desc()).limit(10).all()):
        turns_ = db.query(VoiceTurn).filter(VoiceTurn.call_id == cand.id).order_by(VoiceTurn.turn_no).all()
        if turns_:                      # a scheduled canned call keeps no turns: show the latest call that has some
            last_call, wt = cand, turns_
            break
    if last_call is not None:
        working = {"call_id": last_call.id, "status": last_call.status, "started_at": last_call.created_at,
                   "turns": [{"turn_no": t.turn_no, "speaker": t.speaker, "text": t.text, "kind": t.kind}
                             for t in wt[-8:]], "total_turns": len(wt)}
    # the merchant's shared knowledge (a separate store: not about this customer)
    from app.rag_models import KnowledgeChunk, KnowledgeDoc
    knowledge = {"docs": db.query(KnowledgeDoc).filter(KnowledgeDoc.status == "approved").count(),
                 "approved_passages": db.query(KnowledgeChunk).filter(KnowledgeChunk.status == "approved").count()}
    return {
        "working": working, "records": memory_mod.structured_context(db, None, target),
        "notes": memory_mod.notes_for_contact(db, cp.id), "knowledge": knowledge,
        "memory_budget_chars": settings.memory_budget_chars, "half_life_days": settings.memory_half_life_days,
        "contact_id": cp.id, "memory_consent": bool(cp.memory_consent),
        "memory_consent_current": contacts.has_full_memory_consent(cp),
        "consent_version": cp.memory_consent_version, "current_version": contacts.MEMORY_CONSENT_TEXT_VERSION,
        "retention_days": settings.memory_retention_days, "core_card": core, "tiers": _tier_counts(db, cp.id),
        "facts": [{"id": f.id, "key": f.key, "value": f.value, "status": f.status, "confidence": f.confidence,
                   "importance": f.importance, "valid_from": f.valid_from, "valid_to": f.valid_to,
                   "last_confirmed": f.last_confirmed} for f in facts],
        "episodes": [{"id": e.id, "call_id": e.call_id, "started_at": e.started_at, "outcome": e.outcome,
                      "summary": e.summary, "summary_source": e.summary_source, "tone_arc": e.tone_arc,
                      "key_events": json.loads(e.key_events) if e.key_events else [], "importance": e.importance,
                      "chunks": chunks.get(e.id, [])} for e in episodes],
        "links": [{"relation": l.relation, "src": f"{l.src_type}:{l.src_id[:8]}", "dst": f"{l.dst_type}:{l.dst_id[:8]}"}
                  for l in links],
        "recent_turns": turns,
    }


@router.get("/memory/probe")
def memory_probe(subject_type: str, subject_id: str, q: str, db: Session = Depends(get_db)):
    """What the agent would be handed for this sentence (cue detection, profile, recalled items, scores)."""
    target = _contact_for(db, subject_type, subject_id)
    m = memory_retrieval.build(db, target.contact, q, clock.now())
    return {"cue": bool(m.trace.get("cue")), "full_consent": contacts.has_full_memory_consent(target.contact),
            "prompt_lines": m.prompt_lines(),
            "recalled": [{k: r[k] for k in ("mid", "kind", "when", "text", "score")} for r in m.recalled]}


@router.post("/memory/forget")
def memory_forget(req: PaidIn, db: Session = Depends(get_db)):
    adapter = voice._ADAPTERS.get(req.subject_type)
    target = adapter(db, req.subject_id, clock.now()) if adapter else None
    if target is None or target.contact is None:
        raise HTTPException(404, "Unknown subject")
    return {"deleted": memory_mod.forget(db, target.contact.id, reason="demo_console")}


@router.get("/personas")
def personas_list(db: Session = Depends(get_db)):
    """The seeded test customers (and whether each exists yet)."""
    return demo_personas.list_personas(db)


@router.post("/personas/seed")
def personas_seed(db: Session = Depends(get_db)):
    """Create the test customers that are missing; existing ones are left exactly as they are."""
    return demo_personas.seed_all(db)


@router.post("/personas/{key}/reset")
def personas_reset(key: str, db: Session = Depends(get_db)):
    """Put one test customer back to its seeded state (memory, calls, promises, consent)."""
    return demo_personas.reset_persona(db, key)


@router.get("/speech/filler")
def speech_filler(lang: str = "hinglish", n: int = 0):
    """A short "one moment" clip (cached), played while a reply is being prepared."""
    pool = persona.THINKING.get(lang) or persona.THINKING["hinglish"]
    text = persona.apply_gender(pool[n % len(pool)])
    try:
        audio, _ = speech.synthesize_cached(text, lang, None)
    except speech.SpeechError as e:
        raise HTTPException(e.status, e.message)
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/speech/config")
def speech_config():
    return speech.config()


@router.post("/speech/tts")
def speech_tts(call_id: str, turn: int | None = None, db: Session = Depends(get_db)):
    """Speak a call's own script. Takes the call id, not free text, so this
    cannot be used to burn the free voice allowance on arbitrary text."""
    try:
        uuid.UUID(call_id)
    except ValueError:
        raise HTTPException(404, "Unknown call")
    c = db.query(VoiceCall).filter(VoiceCall.id == call_id).first()
    if c is None:
        raise HTTPException(404, "Unknown call")
    text, kind = c.script_text, None
    if turn:
        row = voice_chat.agent_turn(db, call_id, turn)
        if row is None:
            raise HTTPException(404, "Unknown turn")
        text, kind = row.text, row.kind
    t0 = time.perf_counter()
    try:
        approach = voice_chat.call_approach(db, c)
        style, degree = persona.style_for(text, kind, approach)
        audio, hit = speech.synthesize_cached(text, c.language, style, degree, approach)
    except speech.SpeechError as e:
        raise HTTPException(e.status, e.message)
    if turn:
        voice_chat.record_tts_timing(db, call_id, turn, int((time.perf_counter() - t0) * 1000), hit)
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/speech/stt")
async def speech_stt(request: Request, language: str = "hinglish", call_id: str | None = None,
                     db: Session = Depends(get_db)):
    """Listen to one clip. With `call_id` the merchant name, invoice reference and
    amount are added to Whisper's prompt so they are spelled as we know them.
    A clip that is too short / silent / a known phantom comes back with empty
    text and a `reason`, not an error - the live call just keeps listening."""
    audio = await request.body()
    bias = ""
    if call_id:
        try:
            uuid.UUID(call_id)
            c = db.query(VoiceCall).filter(VoiceCall.id == call_id).first()
            adapter = voice._ADAPTERS.get(c.subject_type) if c else None
            target = adapter(db, c.subject_id, clock.now()) if adapter else None
            if target is not None:
                bias = f"{messaging.MERCHANT_NAME}. {target.ref}. {voice._inr(target.amount_inr)}."
        except ValueError:
            pass
    try:
        out = await run_in_threadpool(
            speech.transcribe_clip, audio, request.headers.get("content-type", ""), language, bias)
    except speech.SpeechError as e:
        raise HTTPException(e.status, e.message)
    return out


class TurnIn(BaseModel):
    transcript: str = ""
    stt_ms: int | None = None        # how long listening took, measured by the browser
    interrupted: bool = False        # the customer talked over the previous agent line


@router.post("/calls/{call_id}/turn")
def call_turn(call_id: str, req: TurnIn, db: Session = Depends(get_db)):
    """One line of a back-and-forth call. Questions get fixed answers filled
    with the debt's own facts; a decision (pay / promise / dispute ...) or a
    question the agent cannot answer ends the call."""
    out = voice_chat.converse_turn(db, call_id, req.transcript, stt_ms=req.stt_ms, interrupted=req.interrupted)
    if not out.get("ok"):
        raise HTTPException(409, out.get("error", "call_failed"))
    if out.get("done"):
        text, tone = demo_text.outcome_text(out.get("routed_to"))
        if (out.get("routed_to") or "").endswith("date_too_far"):
            text += (f" Promises can be at most {runtime_flags.get_promise_max_days_b2c()} days ahead "
                     f"for consumers ({runtime_flags.get_promise_max_days_b2b()} for businesses).")
        out = {**out, "explanation": text, "tone": tone}
    return out


@router.get("/calls/ringing")
def calls_ringing(db: Session = Depends(get_db)):
    rows = db.query(VoiceCall).filter(VoiceCall.status == "in_progress").order_by(
        VoiceCall.created_at.desc()).all()
    return [{"call_id": c.id, "subject_type": c.subject_type, "subject_id": c.subject_id,
             "script": c.script_text, "language": c.language,
             "caller": messaging.MERCHANT_NAME,
             "phone_masked": contacts.mask_phone(c.phone)} for c in rows]


@router.get("/calls")
def calls(limit: int = 30, db: Session = Depends(get_db)):
    rows = db.query(VoiceCall).filter(VoiceCall.status != "in_progress").order_by(
        VoiceCall.created_at.desc()).limit(limit).all()
    return [{"id": c.id, "subject_type": c.subject_type, "subject_id": c.subject_id,
             "language": c.language, "status": c.status, "script": c.script_text,
             "transcript": c.transcript, "intent": c.intent, "intent_source": c.intent_source,
             "promise_date": c.extracted_promise_date.isoformat() if c.extracted_promise_date else None,
             "routed_to": c.routed_to, "created_at": c.created_at} for c in rows]

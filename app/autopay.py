"""
Card mandates on Razorpay's recurring API, debited by US ("we charge").

Decided 2026-10-20 over Razorpay Subscriptions: with Subscriptions Razorpay
retries on its own fixed days, so a promise ("I'll have money on Friday") and
our retry rules could only ever be reminders. Here Razorpay holds the mandate
(token), sends the bank pre-debit notice and moves the money - but a debit
only happens when our scheduler asks for it, so a promise moves a REAL debit.

Lifecycle:
  create_mandate      Razorpay customer + hosted registration link (₹1 card
                      authorisation). Mandate row in SETUP.
  token.confirmed     -> ACTIVE (or sync_mandate, which pulls the same facts -
                      works without a webhook tunnel). The first cycle is queued.
  MANDATE_PDN job     creates the debit order with a `notification` object:
                      the bank pre-debit notice starts now and Razorpay refuses
                      the debit before payment_after. >= 37h before the debit
                      (rules_engine.CARD_TOKEN_DEBIT_LEAD). Queues MANDATE_DEBIT.
  MANDATE_DEBIT job   re-checks everything, then POST /payments/create/recurring.
  payment.captured    total_revenue once; an open run settles as recovered;
                      a cycle debit queues the next cycle.
  payment.failed      webhook.py turns it into a SUBSCRIPTION_PENDING Event for
                      the token, so mandate.process_mandate_event opens the run
                      with the usual notices / voice call / promise / sweep -
                      only the retry itself goes through PDN + DEBIT here.
  token.cancelled / .paused  -> every queued debit is cancelled; an open run
                      stops retrying (HALTED) and lapses via the sweep.

With a `notification` object Razorpay does NOT auto-retry a failed debit, so
nothing here can be charged twice behind our back. The cases that could double
charge are guarded: "pay now" through a Payment Link settles the run and
cancels queued debits; MANDATE_DEBIT refuses on a settled run or a paid link;
a debit that still captures on a settled run is flagged
`duplicate_mandate_payment` (refund by a person), never booked.

The ₹1 authorisation payment is audited, not booked as revenue: it is the
mandate's registration charge, not a sale.

Time: our scheduling reads clock.now() (the Demo Console can fast-forward);
Razorpay's payment_after is REAL time, converted at the boundary. Test Mode
sends no pre-debit notice and settles a debit ~5 minutes after it is created
(razorpay.com/docs/payments/recurring-payments/cards/test-cards).
"""
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import clock, payment_links, razorpay_client as rz
from app.audit import write_audit_entry
from app.database import get_db
from app.models import (
    Mandate, MandateRun, MandateState, MandateStatus, PaymentLink, PaymentLinkStatus,
    ScheduledAction, ScheduledKind, ScheduledStatus,
)
from app.rules_engine import CARD_TOKEN_DEBIT_LEAD
from app.scheduler import cancel_for_subject, register_handler, schedule

logger = logging.getLogger(__name__)
router = APIRouter()

PAYMENT_KINDS = ("mandate_debit", "mandate_auth")
CYCLE_SUBJECT = "mandate"          # a scheduled cycle debit (no failure yet)
RUN_SUBJECT = "mandate_run"        # a retry of a failed cycle (mandate.SUBJECT)
_FREQ = {"weekly": timedelta(days=7), "monthly": timedelta(days=30), "yearly": timedelta(days=365)}
_MAX_AMOUNT_FACTOR = 2             # mandate ceiling = 2 x one cycle


# ── Lookups ──────────────────────────────────────────────────────────────────

def token_of(payment_entity: dict) -> str | None:
    notes = payment_entity.get("notes") if isinstance(payment_entity.get("notes"), dict) else {}
    return payment_entity.get("token_id") or notes.get("rr_token")


def mandate_for_token(db: Session, token_id: str | None) -> Mandate | None:
    if not token_id:
        return None
    return db.query(Mandate).filter(Mandate.token_id == token_id).first()


def classify(db: Session, payment_entity: dict) -> str | None:
    """Fallback when a payment carries none of our notes: the ₹1 authorisation
    is paid against the registration invoice; a debit against a known token."""
    inv_id = payment_entity.get("invoice_id")
    if inv_id and db.query(Mandate).filter(Mandate.registration_invoice_id == inv_id).first():
        return "mandate_auth"
    if payment_entity.get("recurring") and mandate_for_token(db, payment_entity.get("token_id")):
        return "mandate_debit"
    return None


def _real_after(debit_at: datetime) -> datetime:
    """clock time -> real time for Razorpay (they differ only on the Demo Console)."""
    return datetime.utcnow() + max(timedelta(0), debit_at - clock.now())


def _is_timing_refusal(e: "rz.RazorpayError") -> bool:
    """Razorpay refusing a debit because its own real-time window has not opened yet (the Demo Console clock ran ahead of
    real time). Two wordings have been seen: the PROMISED_DEBIT_DATE code and "Debit can be attempted 36 hours after
    sending the pre-debit notification"."""
    text = f"{e.code} {e.description}".lower()
    return "promised_debit_date" in text or "hours after sending the pre-debit" in text or "after sending the pre-debit" in text


def _open_run_for(db: Session, mandate: Mandate) -> MandateRun | None:
    return (db.query(MandateRun)
            .filter(MandateRun.mandate_id == mandate.id, MandateRun.risk_settled.is_(False))
            .order_by(MandateRun.opened_at.desc()).first())


# ── Setup ────────────────────────────────────────────────────────────────────

def _registration(db: Session, m: Mandate) -> None:
    now = datetime.utcnow()
    stamp = now.strftime("%y%m%d%H%M%S")
    inv = rz.create_registration_link(
        customer={"name": m.customer_name, "email": m.email, "contact": m.contact},
        description=f"Autopay for {m.customer_name}: up to ₹{m.max_amount_inr:,.0f} per debit",
        max_amount_inr=m.max_amount_inr, frequency=m.frequency if m.frequency in _FREQ else "as_presented",
        token_expire_at=now + timedelta(days=365 * 2), link_expire_at=now + timedelta(days=7),
        receipt=f"mdt-{m.id[:8]}-{stamp}", notes={"rr_kind": "mandate_auth", "rr_mandate": m.id})
    m.registration_invoice_id, m.registration_url = inv.get("id"), inv.get("short_url")
    if not m.razorpay_customer_id:
        m.razorpay_customer_id = inv.get("customer_id")
    m.status = MandateState.SETUP.value
    db.commit()


def create_mandate(db: Session, *, name: str, phone: str | None, email: str, amount_inr: float,
                   frequency: str = "monthly", first_due_at: datetime | None = None,
                   customer_id: str | None = None, service_key: str | None = None,
                   service_name: str | None = None) -> Mandate:
    """Razorpay refuses a card registration link without an email
    ("The email field is required for recurring links"), so it is required."""
    from app import contacts
    if not email:
        raise rz.RazorpayError(400, "email_required", "an email is required for a card mandate")
    m = Mandate(customer_name=name, contact=phone, email=email, amount_inr=round(amount_inr, 2),
                max_amount_inr=round(amount_inr * _MAX_AMOUNT_FACTOR, 2), frequency=frequency,
                customer_id=customer_id, service_key=service_key, service_name=service_name,
                next_due_at=first_due_at or (clock.now() + CARD_TOKEN_DEBIT_LEAD + timedelta(hours=1)))
    db.add(m)
    db.commit()
    db.refresh(m)
    try:
        cust = rz.create_customer(name=name, contact=phone, email=email, notes={"rr_mandate": m.id})
        m.razorpay_customer_id = cust.get("id")
        db.commit()
        _registration(db, m)
    except rz.RazorpayError:
        # Nothing was authorised and nothing references the row yet: don't
        # leave a half-made mandate on the list.
        db.delete(m)
        db.commit()
        raise
    if phone:
        # The number goes on file the way a merchant enters it: unverified, no
        # consent (contacts.py rules - only the customer can tick consent).
        # A signed-in customer already has a ContactPoint (verified at sign-up):
        # link the Razorpay customer to THAT row, so the consent they give on
        # the autopay page is the one the failed-debit call will check.
        contacts.upsert_contact(db, customer_id=customer_id, razorpay_customer_id=m.razorpay_customer_id,
                                phone=phone)
    write_audit_entry(db, "mandate_setup_started", {
        "mandate_id": m.id, "razorpay_customer_id": m.razorpay_customer_id,
        "registration_invoice_id": m.registration_invoice_id, "amount_inr": m.amount_inr,
        "frequency": frequency})
    return m


def _activate(db: Session, m: Mandate, token_id: str, via: str) -> bool:
    moved = (db.query(Mandate).filter(Mandate.id == m.id, Mandate.status != MandateState.ACTIVE.value)
             .update({"status": MandateState.ACTIVE.value, "token_id": token_id}, synchronize_session=False))
    db.commit()
    if not moved:
        return False
    db.refresh(m)
    write_audit_entry(db, "mandate_activated", {"mandate_id": m.id, "token_id": token_id, "via": via})
    run = _open_run_for(db, m)
    if run is not None and run.status in (MandateStatus.HALTED.value, MandateStatus.EXHAUSTED.value):
        # Re-mandated after a halt: the failed cycle gets one debit on the new
        # token, still with the full notice lead. Its run now follows the token.
        run.subscription_id, run.status = token_id, MandateStatus.ACTIVE.value
        db.commit()
        schedule_debit(db, m, clock.now() + CARD_TOKEN_DEBIT_LEAD, run=run, attempt_no=run.attempts_made + 1)
    elif m.frequency in _FREQ and m.next_due_at:
        schedule_debit(db, m, max(m.next_due_at, clock.now() + CARD_TOKEN_DEBIT_LEAD))
    return True


def sync_mandate(db: Session, m: Mandate) -> dict:
    """Pull state from Razorpay (registration paid? token confirmed?) - the
    same facts the webhooks carry, for setups without a tunnel."""
    out = {"mandate_id": m.id, "status_before": m.status}
    if m.status in (MandateState.SETUP.value, MandateState.REJECTED.value) and m.razorpay_customer_id:
        inv = rz.fetch_invoice(m.registration_invoice_id) if m.registration_invoice_id else {}
        out["registration_status"] = inv.get("status")
        for t in rz.list_tokens(m.razorpay_customer_id):
            state = (t.get("recurring_details") or {}).get("status") or ("confirmed" if t.get("recurring") else None)
            if state == "confirmed" and t.get("id") and t["id"] != m.token_id:
                _activate(db, m, t["id"], via="sync")
                break
    out["status"] = m.status
    return out


def handle_token_event(db: Session, event_name: str, inner_payload: dict) -> dict:
    tok = inner_payload.get("token", {}).get("entity", {})
    token_id, cust_id = tok.get("id"), tok.get("customer_id")
    m = mandate_for_token(db, token_id)
    if m is None and cust_id:
        m = (db.query(Mandate).filter(Mandate.razorpay_customer_id == cust_id)
             .order_by(Mandate.created_at.desc()).first())
    if m is None:
        write_audit_entry(db, "token_event_unmatched", {"event": event_name, "token_id": token_id})
        return {"status": "unmatched"}
    if event_name == "token.confirmed":
        return {"status": "processed", "activated": _activate(db, m, token_id, via="webhook")}
    state = {"token.rejected": MandateState.REJECTED, "token.cancelled": MandateState.CANCELLED,
             "token.paused": MandateState.PAUSED}.get(event_name)
    if state is None:
        return {"status": "ignored"}
    if m.token_id and token_id and m.token_id != token_id:
        # An older token of a re-mandated customer - the live one is fine.
        return {"status": "ignored_old_token"}
    m.status = state.value
    db.commit()
    stop_debits(db, m, reason=event_name)
    write_audit_entry(db, "mandate_state_changed", {"mandate_id": m.id, "token_id": token_id,
                                                    "status": state.value})
    return {"status": "processed", "mandate_status": state.value}


def stop_debits(db: Session, m: Mandate, reason: str) -> None:
    kinds = [ScheduledKind.MANDATE_PDN, ScheduledKind.MANDATE_DEBIT]
    cancel_for_subject(db, CYCLE_SUBJECT, m.id, reason=reason, kinds=kinds)
    run = _open_run_for(db, m)
    if run is not None:
        cancel_for_subject(db, RUN_SUBJECT, run.id, reason=reason,
                           kinds=kinds + [ScheduledKind.MANDATE_RETRY, ScheduledKind.PRE_DEBIT_NOTICE])
        if run.status == MandateStatus.ACTIVE.value:
            # cannot be debited any more: wait for a re-mandate, else the sweep
            run.status = MandateStatus.HALTED.value
            from app import runtime_flags
            run.next_action_at = clock.now() + timedelta(days=runtime_flags.get_mandate_retry_window_days())
            db.commit()


def remandate_url(db: Session, run: MandateRun) -> str | None:
    """A fresh registration link on the same Mandate (its next token replaces
    the old one; _activate then retries the halted run on it)."""
    m = db.query(Mandate).filter(Mandate.id == run.mandate_id).first()
    if m is None:
        return None
    try:
        _registration(db, m)
    except rz.RazorpayError as e:
        write_audit_entry(db, "mandate_registration_link_failed", {
            "mandate_id": m.id, "mandate_run_id": run.id, "razorpay_code": e.code})
        return None
    return m.registration_url


# ── Simulated debit results (Demo Console) ───────────────────────────────────
# Test Mode cannot make a LATER debit happen on demand (Razorpay's notice window
# and the token's expiry check run on real time, the Demo Console clock does not).
# So the console can ask for a debit's RESULT to be played instead of calling
# Razorpay: a payment.captured / payment.failed that goes through exactly the
# code a real webhook does (webhook.deliver_event), minus the signature check.
# Labelled everywhere (`rr_simulated` note, `mandate_debit_simulated` audit).

_sim_outcome: ContextVar[str | None] = ContextVar("autopay_sim_outcome", default=None)
SIM_OUTCOMES = ("success", "insufficient_funds")


@contextmanager
def simulate_debits(outcome: str):
    if outcome not in SIM_OUTCOMES:
        raise ValueError(f"outcome must be one of {SIM_OUTCOMES}")
    tok = _sim_outcome.set(outcome)
    try:
        yield
    finally:
        _sim_outcome.reset(tok)


def simulated_outcome() -> str | None:
    """The result to play instead of calling Razorpay, or None for the real call."""
    from app import runtime_flags
    return _sim_outcome.get() or runtime_flags.get_sim_debit_outcome()


def play_simulated_result(db: Session, m: Mandate, outcome: str, *, amount_inr: float, notes: dict,
                          order_id: str | None) -> str:
    """Deliver a captured / failed payment for this mandate through the webhook path."""
    from fastapi import BackgroundTasks
    from app import webhook
    pid = f"pay_sim_{uuid.uuid4().hex[:14]}"
    pay = {"id": pid, "entity": "payment", "amount": int(round(amount_inr * 100)), "currency": "INR",
           "method": "card", "order_id": order_id or f"order_sim_{uuid.uuid4().hex[:10]}",
           "token_id": m.token_id, "customer_id": m.razorpay_customer_id,
           "notes": {**notes, "rr_simulated": "1"}}
    if outcome == "success":
        event, pay["status"] = "payment.captured", "captured"
    else:
        event, pay["status"] = "payment.failed", "failed"
        pay.update({"error_code": "BAD_REQUEST_ERROR", "error_source": "bank", "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                    "error_description": "Payment failed because the account has insufficient funds"})
    payload = {"event": event, "payload": {"payment": {"entity": pay}}}
    raw = json.dumps(payload).encode()
    bt = BackgroundTasks()
    webhook.deliver_event(db, payload, raw, f"sim_{uuid.uuid4().hex}", bt)
    for t in bt.tasks:                       # what FastAPI does after the response, here and now
        t.func(*t.args, **t.kwargs)
    write_audit_entry(db, "mandate_debit_simulated", {
        "mandate_id": m.id, "payment_id": pid, "outcome": outcome, "amount_inr": amount_inr,
        "note": "a played result - Razorpay was not asked to move money"})
    return pid


# ── Debits ───────────────────────────────────────────────────────────────────

def schedule_debit(db: Session, m: Mandate, debit_at: datetime, run: MandateRun | None = None,
                   attempt_no: int = 1, promise_id: str | None = None) -> ScheduledAction:
    """Queue the PDN for a debit at `debit_at` (it queues the debit itself).
    A run's retry is filed under the run, so everything that already cancels a
    run's jobs (promise opened, settled, superseded) cancels these too."""
    debit_at = max(debit_at, clock.now() + CARD_TOKEN_DEBIT_LEAD)
    pdn_at = max(clock.now(), debit_at - CARD_TOKEN_DEBIT_LEAD)
    st, sid = (RUN_SUBJECT, run.id) if run else (CYCLE_SUBJECT, m.id)
    return schedule(db, ScheduledKind.MANDATE_PDN, st, sid, pdn_at,
                    payload={"mandate_id": m.id, "debit_at": debit_at.isoformat(),
                             "purpose": "retry" if run else "cycle", "promise_id": promise_id},
                    customer_ref=m.razorpay_customer_id, attempt_no=attempt_no)


def _mandate_from(db: Session, payload: dict) -> Mandate | None:
    return db.query(Mandate).filter(Mandate.id == payload.get("mandate_id")).first()


def _run_still_open(db: Session, action: ScheduledAction) -> tuple[bool, str]:
    if action.subject_type != RUN_SUBJECT:
        return True, ""
    run = db.query(MandateRun).filter(MandateRun.id == action.subject_id).first()
    if run is None or run.risk_settled or run.status != MandateStatus.ACTIVE.value:
        return False, "run_not_active"
    paid = (db.query(PaymentLink).filter(PaymentLink.subject_type == RUN_SUBJECT,
                                         PaymentLink.subject_id == run.id,
                                         PaymentLink.amount_paid_inr > 0).first())
    if paid is not None:
        return False, "paid_by_link"
    return True, ""


def _debit_amount(db: Session, action: ScheduledAction, m: Mandate) -> float:
    if action.subject_type == RUN_SUBJECT:
        run = db.query(MandateRun).filter(MandateRun.id == action.subject_id).first()
        if run is not None and run.amount_inr > 0:
            return run.amount_inr
    return m.amount_inr


def _handle_pdn(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    m = _mandate_from(db, payload)
    if m is None or m.status != MandateState.ACTIVE.value or not m.token_id:
        return ScheduledStatus.SKIPPED.value, "mandate_not_active"
    ok, why = _run_still_open(db, action)
    if not ok:
        return ScheduledStatus.SKIPPED.value, why
    debit_at = datetime.fromisoformat(payload["debit_at"])
    amount = _debit_amount(db, action, m)
    notes = {"rr_kind": "mandate_debit", "rr_mandate": m.id, "rr_token": m.token_id,
             "rr_purpose": payload.get("purpose", "cycle"),
             "rr_run": action.subject_id if action.subject_type == RUN_SUBJECT else ""}
    if simulated_outcome():
        schedule(db, ScheduledKind.MANDATE_DEBIT, action.subject_type, action.subject_id, debit_at,
                 payload={**payload, "order_id": None, "amount_inr": amount, "notes": notes},
                 customer_ref=action.customer_ref, attempt_no=action.attempt_no)
        write_audit_entry(db, "mandate_pdn_simulated", {"mandate_id": m.id, "amount_inr": amount,
                                                        "debit_at": debit_at.isoformat()})
        return ScheduledStatus.DONE.value, "pdn_simulated"
    try:
        order = rz.create_debit_order(amount_inr=amount, token_id=m.token_id, debit_at=_real_after(debit_at),
                                      receipt=f"dbt-{action.id[:12]}", notes=notes)
    except rz.RazorpayError as e:
        write_audit_entry(db, "mandate_pdn_failed", {
            "mandate_id": m.id, "subject_type": action.subject_type, "subject_id": action.subject_id,
            "razorpay_code": e.code, "razorpay_error": e.description[:200]})
        return ScheduledStatus.DONE.value, f"pdn_failed:{e.code}"
    write_audit_entry(db, "mandate_pdn_created", {
        "mandate_id": m.id, "order_id": order.get("id"), "amount_inr": amount,
        "debit_at": debit_at.isoformat(), "subject_type": action.subject_type, "subject_id": action.subject_id})
    schedule(db, ScheduledKind.MANDATE_DEBIT, action.subject_type, action.subject_id, debit_at,
             payload={**payload, "order_id": order.get("id"), "amount_inr": amount, "notes": notes,
                      # when Razorpay's (real-time) window opens - the console warns when its clock is ahead of this
                      "real_debit_at": _real_after(debit_at).isoformat()},
             customer_ref=action.customer_ref, attempt_no=action.attempt_no)
    return ScheduledStatus.DONE.value, f"pdn_created:{order.get('id')}"


def _handle_debit(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    m = _mandate_from(db, payload)
    if m is None or m.status != MandateState.ACTIVE.value or not m.token_id:
        return ScheduledStatus.SKIPPED.value, "mandate_not_active"
    ok, why = _run_still_open(db, action)
    if not ok:
        return ScheduledStatus.SKIPPED.value, why
    sim = simulated_outcome()
    if sim:
        pid = play_simulated_result(db, m, sim, amount_inr=payload["amount_inr"],
                                    notes=payload.get("notes") or {}, order_id=payload.get("order_id"))
        return ScheduledStatus.DONE.value, f"debit_simulated:{sim}:{pid}"
    try:
        pay = rz.create_recurring_payment(
            amount_inr=payload["amount_inr"], order_id=payload["order_id"], customer_id=m.razorpay_customer_id,
            token_id=m.token_id, email=m.email or "noreply@example.com", contact=m.contact or "",
            description=f"Autopay {payload.get('purpose', 'cycle')}", notes=payload.get("notes") or {})
    except rz.RazorpayError as e:
        if _is_timing_refusal(e):
            # Razorpay's window is real time; the Demo Console clock ran ahead.
            # Try again in an hour - never dropped.
            schedule(db, ScheduledKind.MANDATE_DEBIT, action.subject_type, action.subject_id,
                     clock.now() + timedelta(hours=1), payload=payload,
                     customer_ref=action.customer_ref, attempt_no=action.attempt_no)
            write_audit_entry(db, "mandate_debit_deferred", {
                "mandate_id": m.id, "order_id": payload.get("order_id"), "reason": "notice_window"})
            return ScheduledStatus.DONE.value, "debit_deferred:notice_window"
        write_audit_entry(db, "mandate_debit_refused", {
            "mandate_id": m.id, "order_id": payload.get("order_id"), "razorpay_code": e.code,
            "razorpay_error": e.description[:200]})
        return ScheduledStatus.DONE.value, f"debit_refused:{e.code}"
    pid = pay.get("razorpay_payment_id") or pay.get("id")
    # "attempted", never "recovered": only payment.captured can say that.
    write_audit_entry(db, "mandate_debit_attempted", {
        "mandate_id": m.id, "payment_id": pid, "order_id": payload.get("order_id"),
        "amount_inr": payload.get("amount_inr"), "purpose": payload.get("purpose"),
        "subject_type": action.subject_type, "subject_id": action.subject_id})
    return ScheduledStatus.DONE.value, f"debit_attempted:{pid}"


register_handler(ScheduledKind.MANDATE_PDN, _handle_pdn)
register_handler(ScheduledKind.MANDATE_DEBIT, _handle_debit)


def handle_payment_captured(db: Session, kind: str, payment: dict) -> dict:
    from app import mandate as mandate_mod
    from app.revenue import adjust
    pid, amount = payment.get("id"), (payment.get("amount") or 0) / 100.0
    notes = payment.get("notes") if isinstance(payment.get("notes"), dict) else {}
    if kind == "mandate_auth":
        write_audit_entry(db, "mandate_authorisation_captured", {
            "payment_id": pid, "amount_inr": amount, "token_id": payment.get("token_id"),
            "note": "registration charge - not booked as revenue"})
        m = db.query(Mandate).filter(Mandate.id == notes.get("rr_mandate")).first() if notes.get("rr_mandate") \
            else db.query(Mandate).filter(Mandate.registration_invoice_id == payment.get("invoice_id")).first()
        if m is not None and payment.get("token_id") and m.status != MandateState.ACTIVE.value:
            # token.confirmed is the authority; pull it rather than assume.
            try:
                sync_mandate(db, m)
            except rz.RazorpayError:
                pass
        return {"status": "processed", "mandate_auth": True}

    m = (db.query(Mandate).filter(Mandate.id == notes.get("rr_mandate")).first() if notes.get("rr_mandate")
         else mandate_for_token(db, token_of(payment)))
    run = None
    if notes.get("rr_run"):
        run = db.query(MandateRun).filter(MandateRun.id == notes["rr_run"]).first()
    elif m is not None:
        run = _open_run_for(db, m)
    if run is not None and run.risk_settled and notes.get("rr_run"):
        # The run was already paid another way (a Payment Link) - extra money.
        write_audit_entry(db, "duplicate_mandate_payment", {
            "payment_id": pid, "amount_inr": amount, "mandate_run_id": run.id,
            "note": "not booked; refund by a person"})
        return {"status": "processed", "duplicate": True}
    adjust(db, "total_revenue", amount, reason="mandate_debit_captured", order_id=pid)
    recovered = False
    if run is not None and not run.risk_settled:
        recovered = mandate_mod._settle(db, run, "recovered", "mandate_debit_captured")
    write_audit_entry(db, "mandate_debit_captured", {
        "payment_id": pid, "amount_inr": amount, "mandate_id": m.id if m else None,
        "mandate_run_id": run.id if run else None, "recovered_run": recovered,
        "purpose": notes.get("rr_purpose")})
    if m is not None and notes.get("rr_purpose") == "cycle" and m.frequency in _FREQ:
        m.next_due_at = (m.next_due_at or clock.now()) + _FREQ[m.frequency]
        db.commit()
        schedule_debit(db, m, m.next_due_at)
    return {"status": "processed", "mandate_debit": True, "recovered_run": recovered}


# ── "Pay now instead": a Payment Link for a failed run ───────────────────────

def run_payment_link(db: Session, run: MandateRun) -> PaymentLink:
    expire_at = datetime.utcnow() + timedelta(days=7)
    return payment_links.ensure_link(db, RUN_SUBJECT, run.id, run.amount_inr, expire_at=expire_at,
                                     description="Pending autopay amount")


def _on_run_link_payment(db: Session, link: PaymentLink, amount_inr: float, payment_id: str | None) -> dict:
    from app import mandate as mandate_mod
    run = db.query(MandateRun).filter(MandateRun.id == link.subject_id).first()
    if run is None:
        return {"mandate_run": "unknown"}
    if run.risk_settled:
        write_audit_entry(db, "duplicate_mandate_payment", {
            "payment_id": payment_id, "amount_inr": amount_inr, "mandate_run_id": run.id,
            "note": "run already settled; refund by a person"})
        return {"mandate_run": run.id, "duplicate": True}
    # _settle cancels the run's queued PDN / debit - no second charge.
    settled = mandate_mod._settle(db, run, "recovered", "paid_by_link")
    return {"mandate_run": run.id, "recovered": settled}


payment_links.register_link_handler(RUN_SUBJECT, _on_run_link_payment)


# ── API (merchant; Demo Console uses it too) ─────────────────────────────────

class MandateIn(BaseModel):
    name: str
    phone: str | None = None
    email: str          # Razorpay requires it for a card registration link
    amount_inr: float
    frequency: str = "monthly"


def _out(m: Mandate) -> dict:
    return {"id": m.id, "customer_name": m.customer_name, "status": m.status, "amount_inr": m.amount_inr,
            "max_amount_inr": m.max_amount_inr, "frequency": m.frequency,
            "razorpay_customer_id": m.razorpay_customer_id, "token_id": m.token_id,
            "registration_url": m.registration_url, "next_due_at": m.next_due_at, "created_at": m.created_at}


@router.post("/autopay/mandates")
def create_mandate_endpoint(req: MandateIn, db: Session = Depends(get_db)):
    from app.otp import OtpError, normalize_phone
    if req.amount_inr < 1:
        raise HTTPException(400, "amount_inr must be at least 1")
    if req.frequency not in (*_FREQ, "as_presented"):
        raise HTTPException(400, "frequency must be weekly, monthly, yearly or as_presented")
    phone = None
    if req.phone:
        try:
            phone = normalize_phone(req.phone)
        except OtpError as e:
            raise HTTPException(400, e.message)
    try:
        m = create_mandate(db, name=req.name, phone=phone, email=req.email, amount_inr=req.amount_inr,
                           frequency=req.frequency)
    except rz.RazorpayError as e:
        raise HTTPException(502, f"Razorpay refused: {e.code} {e.description}")
    return _out(m)


@router.get("/autopay/mandates")
def list_mandates(db: Session = Depends(get_db)):
    return [_out(m) for m in db.query(Mandate).order_by(Mandate.created_at.desc()).limit(100).all()]


def _get(db: Session, mandate_id: str) -> Mandate:
    m = db.query(Mandate).filter(Mandate.id == mandate_id).first()
    if m is None:
        raise HTTPException(404, "mandate not found")
    return m


@router.post("/autopay/mandates/{mandate_id}/sync")
def sync_endpoint(mandate_id: str, db: Session = Depends(get_db)):
    try:
        return sync_mandate(db, _get(db, mandate_id))
    except rz.RazorpayError as e:
        raise HTTPException(502, f"Razorpay refused: {e.code} {e.description}")


@router.post("/autopay/mandates/{mandate_id}/charge-next")
def charge_next(mandate_id: str, db: Session = Depends(get_db)):
    """Queue the next cycle debit at the earliest legal moment (now + the
    37h notice lead). On the Demo Console, fast-forward the clock to fire it."""
    m = _get(db, mandate_id)
    if m.status != MandateState.ACTIVE.value:
        raise HTTPException(409, f"mandate is {m.status}, not active")
    row = schedule_debit(db, m, clock.now() + CARD_TOKEN_DEBIT_LEAD)
    return {"scheduled_pdn": row.id, "pdn_at": row.due_at, "debit_at": json.loads(row.payload_json)["debit_at"]}


def cancel_mandate(db: Session, m: Mandate, via: str) -> Mandate:
    """Cancel the token on Razorpay (best effort, audited), stop every queued
    debit and mark the mandate cancelled. `via` is who asked: merchant / customer."""
    if m.token_id and m.razorpay_customer_id:
        try:
            rz.cancel_token(m.razorpay_customer_id, m.token_id)
        except rz.RazorpayError as e:
            write_audit_entry(db, "mandate_cancel_failed", {"mandate_id": m.id, "razorpay_code": e.code})
    m.status = MandateState.CANCELLED.value
    db.commit()
    stop_debits(db, m, reason=f"{via}_cancelled")
    write_audit_entry(db, "mandate_state_changed", {"mandate_id": m.id, "status": m.status, "via": via})
    return m


@router.post("/autopay/mandates/{mandate_id}/cancel")
def cancel_endpoint(mandate_id: str, db: Session = Depends(get_db)):
    return _out(cancel_mandate(db, _get(db, mandate_id), "merchant"))

"""
Mandate retry sequencer: recovery for failed RECURRING debits (UPI Autopay /
e-mandate / tokenised card).

Before this module subscription.pending / .halted / .charged all fell into the
one-off payment-failure path: a *successful* subscription.charged got a
"retry" decision, "retry later" was only a label, and subscription money was
never at risk anywhere. A recurring debit is a different problem - the failure
is usually timing (salary lands after the debit date), and the right response
is a dated retry plan plus the pre-debit notice RBI requires, not "retry now".

Who owns retries:
- Razorpay Subscriptions: Razorpay retries on its own and tells us via
  subscription.pending / .halted. Our job there is communications (pre-debit
  notice, re-mandate link) and deciding when to stop waiting.
- Merchant-driven debits on a stored mandate/token: the merchant owns the
  timing, so `rule_based_mandate_plan` (rules_engine.py) picks the retry date
  and the scheduler (scheduler.py) fires it.

Money: each failed cycle is a MandateRun. Its full amount is booked once into
`at_risk_mandate` when the run opens and leaves exactly once via
`risk_settled` (atomic claim) to total_recovered or total_lost. This thread
never touches the cart / declined / failed-payment buckets.

The LLM is deliberately not involved: dates and caps are policy. Like the
cart-event path this is rules-only, so there is no llm_failure_fallback here.

Retries: for a card mandate WE debit (MandateRun.mandate_id set, app/autopay.py)
a retry is real - a Razorpay debit order with its bank pre-debit notice (>= 37h
ahead, rules_engine.CARD_TOKEN_DEBIT_LEAD) and then the debit; a promise moves
that debit. For a Razorpay Subscription run the retry is Razorpay's own and
MANDATE_RETRY stays a stub label. Nothing here may report "recovered": only a
captured debit (payment.captured / subscription.charged) or a paid Payment Link can.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import clock, runtime_flags
from app.actions import execute_action
from app.audit import write_audit_entry
from app.models import (
    AgentAction, Decision, Event, EventType, FailureReason, MandateRun, MandateStatus,
    ScheduledAction, ScheduledKind, ScheduledStatus,
)
from app.revenue import adjust as adjust_revenue
from app.rules_engine import CARD_TOKEN_DEBIT_LEAD, MANDATE_MIN_RETRY_GAP, rule_based_mandate_plan
from app.database import get_db
from app.scheduler import cancel_for_subject, register_handler, schedule
from app import contacts, messaging, notify, promises

logger = logging.getLogger(__name__)
router = APIRouter()

SUBJECT = "mandate_run"
MANDATE_EVENT_TYPES = (EventType.SUBSCRIPTION_PENDING, EventType.SUBSCRIPTION_HALTED)
_OPEN_STATUSES = (MandateStatus.ACTIVE.value, MandateStatus.HALTED.value, MandateStatus.EXHAUSTED.value)


# ── Money: exactly-once exit ─────────────────────────────────────────────────

def _settle(db: Session, run: MandateRun, outcome: str, reason: str) -> bool:
    """Release a run's at-risk money to recovered or lost, exactly once.
    Returns False (and moves nothing) if some other path already settled it."""
    claimed = (
        db.query(MandateRun)
        .filter(MandateRun.id == run.id, MandateRun.risk_settled.is_(False))
        .update({"risk_settled": True, "closed_at": clock.now()})
    )
    db.commit()
    if not claimed:
        return False
    db.refresh(run)

    if run.risk_booked and run.amount_inr > 0:
        bucket = "total_recovered" if outcome == "recovered" else "total_lost"
        adjust_revenue(db, "at_risk_mandate", -run.amount_inr, reason=f"mandate_{reason}",
                       order_id=run.subscription_id)
        adjust_revenue(db, bucket, run.amount_inr, reason=f"mandate_{reason}",
                       order_id=run.subscription_id)

    run.status = MandateStatus.RECOVERED.value if outcome == "recovered" else MandateStatus.CANCELLED.value
    db.commit()
    cancel_for_subject(db, SUBJECT, run.id, reason=f"run_settled:{reason}")
    # A "pay now" link for this run must not stay payable once it is settled.
    from app import payment_links   # local: payment_links -> autopay -> this module
    payment_links.close_for_subject(db, SUBJECT, run.id, reason=f"run_settled:{reason}")
    # A promise on this debt resolves with it: kept if it was paid, retired if
    # the debt was given up on. Neither moves money (the settle above did).
    if outcome == "recovered":
        promises.resolve_on_payment(db, SUBJECT, run.id, run.amount_inr)
    else:
        promises.close_for_subject(db, SUBJECT, run.id, reason=f"run_settled:{reason}")
    write_audit_entry(db, "mandate_run_recovered" if outcome == "recovered" else "mandate_run_lost", {
        "mandate_run_id": run.id, "subscription_id": run.subscription_id,
        "amount_inr": run.amount_inr, "reason": reason, "attempts_made": run.attempts_made,
    })
    return True


# ── Signal intake ────────────────────────────────────────────────────────────

def _open_run(db: Session, subscription_id: str) -> MandateRun | None:
    return (
        db.query(MandateRun)
        .filter(MandateRun.subscription_id == subscription_id,
                MandateRun.risk_settled.is_(False),
                MandateRun.status.in_(_OPEN_STATUSES))
        .order_by(MandateRun.opened_at.desc())
        .first()
    )


def process_mandate_event(db: Session, event: Event) -> Decision:
    """Decision + action for a subscription.pending / .halted Event. Runs from
    webhook._process_decision_and_action (background), so Razorpay already got
    its 200."""
    now = clock.now()
    sub_id = event.subscription_id or event.razorpay_order_id or event.id
    halted = event.event_type == EventType.SUBSCRIPTION_HALTED
    reason = event.failure_reason or FailureReason.UNKNOWN

    from app import autopay   # local: autopay imports this module's _settle at call time
    card = autopay.mandate_for_token(db, sub_id)

    run = _open_run(db, sub_id)
    if run is None:
        run = MandateRun(
            subscription_id=sub_id, razorpay_customer_id=event.customer_id or (card.razorpay_customer_id if card else None),
            amount_inr=event.amount_inr or 0.0, failure_reason=reason.value,
            attempts_made=max(1, event.attempt_count or 1), opened_at=now,
            mandate_id=card.id if card else None,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        if run.amount_inr > 0:
            adjust_revenue(db, "at_risk_mandate", run.amount_inr, reason="mandate_run_opened",
                           order_id=sub_id)
            run.risk_booked = True
            db.commit()
        write_audit_entry(db, "mandate_run_opened", {
            "mandate_run_id": run.id, "subscription_id": sub_id,
            "amount_inr": run.amount_inr, "failure_reason": reason.value,
        }, event_id=event.id)
    else:
        # A newer failure on the same cycle. Trust Razorpay's own counter when
        # it is ahead of ours (we may have started watching mid-run).
        run.attempts_made = max(run.attempts_made + 1, event.attempt_count or 0)
        run.failure_reason = reason.value
        if not run.risk_booked and (event.amount_inr or 0) > 0:
            run.amount_inr = event.amount_inr
            adjust_revenue(db, "at_risk_mandate", run.amount_inr, reason="mandate_run_opened",
                           order_id=sub_id)
            run.risk_booked = True
        db.commit()

    # A fresh failure supersedes anything still queued for this run.
    cancel_for_subject(db, SUBJECT, run.id, reason="superseded_by_new_failure")

    plan = rule_based_mandate_plan(
        failure_reason=reason, attempts_made=run.attempts_made, opened_at=run.opened_at, now=now,
        max_retries=runtime_flags.get_mandate_max_retries(),
        window_days=runtime_flags.get_mandate_retry_window_days(), halted=halted,
        min_gap=CARD_TOKEN_DEBIT_LEAD if card else MANDATE_MIN_RETRY_GAP,
    )
    action: AgentAction = plan["action"]

    decision = Decision(
        event_id=event.id, action=action, confidence=plan["confidence"],
        reasoning=plan["reasoning"], source="rules_engine", escalated=False,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    write_audit_entry(db, "decision_made", {
        "action": action.value, "confidence": plan["confidence"], "reasoning": plan["reasoning"],
        "source": "rules_engine", "escalated": False, "mandate_run_id": run.id,
        "terminal": plan["terminal"],
    }, event_id=event.id, decision_id=decision.id)

    if plan["terminal"] is None:
        # A dated retry (and its pre-debit notice) - NOT executed now. The
        # scheduler fires it; the outcome label says when.
        retry_at: datetime = plan["retry_at"]
        _schedule_retry(db, run, retry_at, customer_ref=event.customer_id)
        if plan["notice_at"] is not None:
            schedule(db, ScheduledKind.PRE_DEBIT_NOTICE, SUBJECT, run.id, plan["notice_at"],
                     payload={"subscription_id": sub_id, "retry_at": retry_at.isoformat()},
                     customer_ref=event.customer_id, attempt_no=run.attempts_made)
        # One call in the window before the retry - the natural moment to hear
        # "funds will be there on the 5th" and turn it into a promise. Only
        # queued when we hold a number for this customer; whether it actually
        # RINGS is re-judged when it fires (verified number + customer-ticked
        # consent + window), so consent given in between still counts.
        cp = contacts.for_razorpay_customer(db, event.customer_id)
        call_at = retry_at - timedelta(hours=18)
        if cp is not None and cp.phone and call_at > now + timedelta(hours=1):
            schedule(db, ScheduledKind.VOICE_CALL, SUBJECT, run.id, call_at,
                     payload={"subscription_id": sub_id}, customer_ref=cp.id,
                     attempt_no=run.attempts_made)
        run.next_action_at = retry_at
        run.status = MandateStatus.ACTIVE.value
        success, outcome = True, f"pending_retry_{retry_at:%Y-%m-%d}"
    else:
        success, outcome = execute_action(action.value, event)
        if action == AgentAction.SEND_REMANDATE_LINK:
            msg = notify.mandate_message(db, run, "remandate_link")
            success = messaging.delivered(msg)
            outcome = "remandate_link_sent" if success else f"remandate_link_not_sent:{msg.status}"
        # Not settled here: a re-mandate can still land, and an escalated run
        # may still be rescued by a human - same shape as escalated cancels,
        # which lapse to lost via the sweep rather than at decision time.
        run.status = MandateStatus.HALTED.value if plan["terminal"] == "remandate" \
            else MandateStatus.EXHAUSTED.value
        run.next_action_at = now + timedelta(days=runtime_flags.get_mandate_retry_window_days())
    db.commit()

    decision.executed = True
    decision.outcome = outcome
    db.commit()
    write_audit_entry(db, "action_executed", {
        "action": action.value, "success": success, "outcome": outcome, "mandate_run_id": run.id,
    }, event_id=event.id, decision_id=decision.id)
    return decision


def _schedule_retry(db: Session, run: MandateRun, retry_at: datetime, customer_ref: str | None,
                    promise_id: str | None = None) -> None:
    """A real debit for a card mandate we charge (PDN now-ish, debit at
    retry_at); Razorpay's own retry (a stub label here) for a Subscription run."""
    if run.mandate_id:
        from app import autopay
        from app.models import Mandate
        m = db.query(Mandate).filter(Mandate.id == run.mandate_id).first()
        if m is not None:
            autopay.schedule_debit(db, m, retry_at, run=run, attempt_no=run.attempts_made,
                                   promise_id=promise_id)
            return
    payload = {"subscription_id": run.subscription_id}
    if promise_id:
        payload["promise_id"] = promise_id
    schedule(db, ScheduledKind.MANDATE_RETRY, SUBJECT, run.id, retry_at, payload=payload,
             customer_ref=customer_ref, attempt_no=run.attempts_made)


def handle_subscription_charged(db: Session, inner_payload: dict) -> dict:
    """subscription.charged is good news: close the open run as recovered.

    It deliberately does NOT book total_revenue - Razorpay also sends
    payment.captured for the same debit, and that path already books capture
    revenue; doing it here too would double-count the money."""
    sub_entity = inner_payload.get("subscription", {}).get("entity", {})
    sub_id = sub_entity.get("id") or inner_payload.get("payment", {}).get("entity", {}).get("subscription_id")

    run = _open_run(db, sub_id) if sub_id else None
    if run is None:
        write_audit_entry(db, "subscription_charged", {
            "subscription_id": sub_id, "matched_open_run": False,
        })
        return {"status": "processed", "matched_open_run": False}

    settled = _settle(db, run, "recovered", "subscription_charged")
    return {"status": "processed", "matched_open_run": True, "recovered": settled,
            "mandate_run_id": run.id}


# ── Scheduler handlers ───────────────────────────────────────────────────────

def _live_run(db: Session, action: ScheduledAction) -> MandateRun | None:
    run = db.query(MandateRun).filter(MandateRun.id == action.subject_id).first()
    if run is None or run.risk_settled or run.status != MandateStatus.ACTIVE.value:
        return None
    return run


def _handle_retry(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    run = _live_run(db, action)
    if run is None:
        return ScheduledStatus.SKIPPED.value, "run_not_active"
    # Stubbed call (see actions.py) - success here means "retry issued", never
    # "recovered"; only a later subscription.charged webhook can say that.
    success, outcome = execute_action(AgentAction.RETRY_NOW.value, None)
    write_audit_entry(db, "mandate_retry_attempted", {
        "mandate_run_id": run.id, "subscription_id": run.subscription_id,
        "attempt_no": action.attempt_no, "success": success, "outcome": outcome,
    })
    return ScheduledStatus.DONE.value, outcome


def _handle_notice(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    run = _live_run(db, action)
    if run is None:
        return ScheduledStatus.SKIPPED.value, "run_not_active"
    from app.selfserve import link_path   # local: selfserve imports promises/contacts
    retry_at = None
    if payload.get("retry_at"):
        try:
            retry_at = datetime.fromisoformat(payload["retry_at"])
        except ValueError:
            retry_at = None
    msg = notify.mandate_message(db, run, "pre_debit_notice", retry_at=retry_at)
    sent = messaging.delivered(msg)
    # "sent" only when a channel took it: no number on file / a provider error
    # is its own audit type, so the log never claims a notice that did not go.
    write_audit_entry(db, "pre_debit_notice_sent" if sent else "pre_debit_notice_not_sent", {
        "mandate_run_id": run.id, "subscription_id": run.subscription_id,
        "retry_at": payload.get("retry_at"), "success": sent,
        "message_id": msg.id, "message_status": msg.status,
        # where the customer can pick a date / confirm their number / consent
        "self_serve_link_path": link_path("mandate_run", run.id),
    })
    return ScheduledStatus.DONE.value, ("pre_debit_notice_sent" if sent
                                        else f"pre_debit_notice_not_sent:{msg.status}")


register_handler(ScheduledKind.MANDATE_RETRY, _handle_retry)
register_handler(ScheduledKind.PRE_DEBIT_NOTICE, _handle_notice)


# ── Promise-to-pay integration ───────────────────────────────────────────────

def _promise_subject(db: Session, run_id: str) -> dict | None:
    run = db.query(MandateRun).filter(MandateRun.id == run_id).first()
    if run is None:
        return None
    return {
        "amount_inr": run.amount_inr, "customer_ref": run.razorpay_customer_id,
        "is_open": (not run.risk_settled) and run.status in _OPEN_STATUSES,
        "b2b": False,
        "chase_kinds": [ScheduledKind.MANDATE_RETRY, ScheduledKind.PRE_DEBIT_NOTICE,
                        ScheduledKind.VOICE_CALL, ScheduledKind.MANDATE_PDN, ScheduledKind.MANDATE_DEBIT],
    }


def _on_promise_opened(db: Session, promise) -> None:
    """The chase was paused by promises.create_promise. A failed DEBIT does not
    pay itself, though - the customer saying "I'll have funds by the 5th" is
    only useful if we then actually retry on the 5th. So an ACTIVE run gets its
    retry (and notice) moved to the promised date. Halted/exhausted runs cannot
    be debited; they just get their lapse deadline pushed past the promise."""
    run = db.query(MandateRun).filter(MandateRun.id == promise.subject_id).first()
    if run is None:
        return
    now = clock.now()
    promised_at = datetime(promise.promised_date.year, promise.promised_date.month,
                           promise.promised_date.day, 10, 0) - timedelta(hours=5, minutes=30)
    # same floor as the rules plan: 25h, or the card debit lead for a card mandate
    retry_at = max(promised_at, now + (CARD_TOKEN_DEBIT_LEAD if run.mandate_id else timedelta(hours=25)))
    if run.status == MandateStatus.ACTIVE.value:
        # For a card mandate this moves the REAL debit to the promised day.
        _schedule_retry(db, run, retry_at, customer_ref=run.razorpay_customer_id, promise_id=promise.id)
        schedule(db, ScheduledKind.PRE_DEBIT_NOTICE, SUBJECT, run.id, retry_at - timedelta(hours=24),
                 payload={"subscription_id": run.subscription_id, "retry_at": retry_at.isoformat()},
                 customer_ref=run.razorpay_customer_id, attempt_no=run.attempts_made)
    run.next_action_at = max(run.next_action_at or now, retry_at + timedelta(
        days=runtime_flags.get_promise_grace_days() + 1))
    db.commit()


def _on_promise_broken(db: Session, promise) -> None:
    """Broken promise = the customer's own word failed: stop automatic
    attempts and hand the run to a human (one step firmer). The money stays
    at risk; the sweep releases it to lost if nobody rescues it."""
    run = db.query(MandateRun).filter(MandateRun.id == promise.subject_id).first()
    if run is None or run.risk_settled:
        return
    cancel_for_subject(db, SUBJECT, run.id, reason="promise_broken")
    execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
    run.status = MandateStatus.EXHAUSTED.value
    run.next_action_at = clock.now() + timedelta(days=runtime_flags.get_mandate_retry_window_days())
    db.commit()
    write_audit_entry(db, "mandate_promise_broken_escalated", {
        "mandate_run_id": run.id, "promise_id": promise.id, "amount_inr": run.amount_inr,
    })


promises.register_subject(SUBJECT, _promise_subject)
promises.register_opened_hook(SUBJECT, _on_promise_opened)
promises.register_broken_hook(SUBJECT, _on_promise_broken)


# ── Sweep (called from maintenance.close_dead_recovery_paths) ────────────────

def lapse_due_at(run: MandateRun) -> datetime:
    """When the sweep gives up on an unsettled run and books it as lost."""
    window = timedelta(days=runtime_flags.get_mandate_retry_window_days())
    if run.status == MandateStatus.ACTIVE.value:
        # next_action_at keeps a run alive while a retry (or a promised
        # date) is still ahead of it.
        return max(run.opened_at + window + timedelta(days=1), run.next_action_at or run.opened_at)
    return run.next_action_at or (run.opened_at + window)


def lapse_stale_runs(db: Session, now: datetime | None = None) -> int:
    """Every run's at-risk money must leave through an exit. Runs that will
    never get a charged webhook are closed as lost here:
    - HALTED / EXHAUSTED runs once their grace window (next_action_at) passes
      with no re-mandate and no human rescue;
    - ACTIVE runs that stopped moving (retry fired, no result ever arrived)
      a day after the retry window closes."""
    now = now or clock.now()
    window = timedelta(days=runtime_flags.get_mandate_retry_window_days())
    stale = (
        db.query(MandateRun)
        .filter(MandateRun.risk_settled.is_(False), MandateRun.status.in_(_OPEN_STATUSES))
        .all()
    )
    lapsed = 0
    for run in stale:
        due = lapse_due_at(run)
        if due < now and _settle(db, run, "lost", "lapsed_no_recovery"):
            lapsed += 1
    return lapsed


# ── Dashboard listing ────────────────────────────────────────────────────────

@router.get("/merchant/mandate-runs")
def list_runs(db: Session = Depends(get_db)):
    """Failed recurring debits, with the contact state and the customer's
    self-serve link - what the merchant needs to put a number on file and
    send the customer their page."""
    from app.selfserve import link_path   # local: selfserve imports promises/contacts
    out = []
    rows = db.query(MandateRun).order_by(MandateRun.opened_at.desc()).limit(200).all()
    for r in rows:
        out.append({
            "id": r.id, "subscription_id": r.subscription_id,
            "razorpay_customer_id": r.razorpay_customer_id, "amount_inr": r.amount_inr,
            "status": r.status, "attempts_made": r.attempts_made,
            "next_action_at": r.next_action_at, "opened_at": r.opened_at,
            "contact": contacts.contact_out(contacts.for_razorpay_customer(db, r.razorpay_customer_id)),
            "self_serve_path": link_path(SUBJECT, r.id),
        })
    return out

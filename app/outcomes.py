from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import Event, Decision, Order, Customer
from app.audit import verify_chain
from app.models import AuditLog

router = APIRouter()


@router.get("/outcomes")
def outcomes(db: Session = Depends(get_db)):
    total_events = db.query(func.count(Event.id)).scalar() or 0

    # Nudge-type outcomes are recovery ATTEMPTS, not recoveries - actions.py
    # is explicit that nothing may claim "recovered" except a real
    # payment.captured webhook. Actual recovered/lost MONEY lives in the
    # merchant ledger (/merchant/revenue: total_recovered, total_lost);
    # this endpoint reports decision-outcome counts only, honestly labeled.
    # (The old "recovered" field counted nudge_sent, so the dashboard
    # claimed recoveries no money ever confirmed.)
    nudges_sent = (
        db.query(func.count(Decision.id))
        .filter(
            Decision.outcome.in_(["nudge_sent", "reminder_sent", "resume_link_sent"])
            | Decision.outcome.like("incentive_offered%")
        )
        .scalar() or 0
    )
    # NOTE: Decision.escalated only flags cases where the confidence GATE
    # overrode an original non-escalate action. It does NOT capture cases
    # where the rules engine/LLM decided to escalate on their own merits
    # (e.g. risk_block, max retries exceeded). For a true count of every
    # escalation regardless of source, filter on the outcome label instead.
    escalated = (
        db.query(func.count(Decision.id))
        .filter(Decision.outcome == "escalated")
        .scalar() or 0
    )
    gate_overrides = (
        db.query(func.count(Decision.id))
        .filter(Decision.escalated.is_(True))
        .scalar() or 0
    )
    # Immediate AND delayed retries together - retry_scheduled (the
    # retry_now outcome, the single most common one) used to be counted in
    # NO bucket at all, so the chart quietly dropped it.
    retries = (
        db.query(func.count(Decision.id))
        .filter((Decision.outcome == "retry_scheduled") | Decision.outcome.like("pending_retry%"))
        .scalar() or 0
    )
    no_action = (
        db.query(func.count(Decision.id))
        .filter(Decision.outcome == "no_action_taken")
        .scalar() or 0
    )
    failed = (
        db.query(func.count(Decision.id))
        .filter(Decision.outcome.like("%failed%") | Decision.outcome.like("%no_razorpay%")
                | (Decision.outcome == "unknown_action"))
        .scalar() or 0
    )

    total_decisions = db.query(func.count(Decision.id)).scalar() or 0
    # Residual so a future outcome string can never silently vanish from
    # the dashboard again - anything unbucketed shows up as "other".
    other = max(0, total_decisions - (nudges_sent + retries + escalated + no_action + failed))

    return {
        "total_events": total_events,
        "total_decisions": total_decisions,
        "nudges_sent": nudges_sent,
        "escalated": escalated,
        "escalated_by_confidence_gate": gate_overrides,
        "retries": retries,
        "no_action": no_action,
        "failed": failed,
        "other": other,
    }


@router.get("/outcomes/events")
def list_events(db: Session = Depends(get_db), limit: int = 50):
    events = db.query(Event).order_by(Event.received_at.desc()).limit(limit).all()

    # Resolve the storefront customer behind each event in one batched
    # query: Event.razorpay_order_id -> Order -> Customer. This is the
    # whole reason Event carries razorpay_order_id (see models.py) -
    # Event.customer_id alone is Razorpay's id, which is usually null for
    # one-off storefront payments, so the feed showed no customer at all.
    order_ids = [e.razorpay_order_id for e in events if e.razorpay_order_id]
    names_by_order_id: dict[str, str] = {}
    if order_ids:
        rows = (
            db.query(Order.razorpay_order_id, Customer.name)
            .join(Customer, Customer.id == Order.customer_id)
            .filter(Order.razorpay_order_id.in_(order_ids))
            .all()
        )
        names_by_order_id = dict(rows)

    # A handful of Events are customer-initiated rather than Razorpay-
    # reported (see EventType.PAYMENT_FAILURE_GIVEN_UP) and carry no
    # razorpay_order_id at all - customer_id on those is OUR OWN
    # Customer.id directly, not a Razorpay id. Resolve those too, so a
    # give-up entry shows a name exactly like every other row.
    direct_customer_ids = [e.customer_id for e in events if not e.razorpay_order_id and e.customer_id]
    names_by_customer_id: dict[str, str] = {}
    if direct_customer_ids:
        rows2 = db.query(Customer.id, Customer.name).filter(Customer.id.in_(direct_customer_ids)).all()
        names_by_customer_id = dict(rows2)

    result = []
    for e in events:
        decisions = sorted(e.decisions, key=lambda d: d.created_at)
        latest = decisions[-1] if decisions else None
        result.append({
            "event_id": e.id,
            "event_type": e.event_type,
            "customer_name": names_by_order_id.get(e.razorpay_order_id) or names_by_customer_id.get(e.customer_id),
            "customer_id": e.customer_id,
            "failure_reason": e.failure_reason,
            "attempt_count": e.attempt_count,
            "amount_inr": e.amount_inr,
            "received_at": e.received_at,
            "decision": {
                "action": latest.action if latest else None,
                "confidence": latest.confidence if latest else None,
                "reasoning": latest.reasoning if latest else None,
                "source": latest.source if latest else None,
                "escalated": latest.escalated if latest else None,
                "outcome": latest.outcome if latest else None,
            } if latest else None,
        })
    return result


@router.get("/audit/verify")
def audit_verify(db: Session = Depends(get_db)):
    ok, message = verify_chain(db)
    return {"chain_intact": ok, "message": message}


@router.get("/audit/log")
def audit_log(db: Session = Depends(get_db), limit: int = 100):
    entries = db.query(AuditLog).order_by(AuditLog.sequence_num.desc()).limit(limit).all()
    return [
        {
            "sequence_num": e.sequence_num,
            "action_type": e.action_type,
            "details": e.details,
            "prev_hash": e.prev_hash[:12] + "...",
            "entry_hash": e.entry_hash[:12] + "...",
            "created_at": e.created_at,
        }
        for e in entries
    ]

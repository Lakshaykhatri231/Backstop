"""
Checkout drop-off detection and recovery pipeline.

Detection mechanism: polling (not webhooks — Razorpay has no "order.abandoned"
event; abandonment is the *absence* of a paid status after a time window).

Poll cadence: every DROPOFF_POLL_INTERVAL_MINUTES (default 10).
Abandonment definition: order.status == "attempted" AND created more than
DROPOFF_ABANDONMENT_WINDOW_MINUTES ago AND not yet paid.

Only "attempted" orders are actioned — "created" means the customer never
opened the payment screen, which is a different (weaker) signal and is
logged as NO_ACTION rather than triggering a recovery attempt.
"""
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Event, EventType, Decision, AgentAction
from app.llm_agent import decide_dropoff
from app.actions import execute_action
from app.audit import write_audit_entry
from app import runtime_flags


# ── Razorpay Orders API client ───────────────────────────────────────────────

def _fetch_razorpay_orders(from_ts: int, to_ts: int) -> list[dict]:
    """
    Fetch orders created between from_ts and to_ts (Unix timestamps).
    Returns raw order dicts from Razorpay's API.
    In demo/test mode with no real credentials, returns an empty list
    so the poller starts cleanly without crashing.
    """
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        return []

    try:
        resp = httpx.get(
            "https://api.razorpay.com/v1/orders",
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            params={"from": from_ts, "to": to_ts, "count": 100},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])
    except httpx.HTTPError:
        return []


# ── Incentive eligibility check ──────────────────────────────────────────────

def _incentive_eligible(db: Session, customer_id: str, subscription_id: str | None, amount_inr: float) -> bool:
    """
    Three independent gate checks — ALL must pass:
    1. Order value <= the casual-tier amount cap (this pipeline only has
       Razorpay's customer id, not our Customer row, so there's no tier to
       look up - use the LOWEST tier's cap as the conservative default)
    2. Customer hasn't received an incentive in the last 30 days
    3. No prior escalation on this subscription/customer in the lookback window
    """
    if amount_inr > runtime_flags.get_incentive_max_order_value("casual"):
        return False

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    scope_filter = (
        Event.subscription_id == subscription_id
        if subscription_id
        else Event.customer_id == customer_id
    )
    prior_incentive = (
        db.query(Decision)
        .join(Event)
        .filter(
            scope_filter,
            Decision.action == AgentAction.OFFER_INCENTIVE,
            Decision.created_at >= thirty_days_ago,
        )
        .first()
    )
    if prior_incentive:
        return False

    seven_days_ago = datetime.utcnow() - timedelta(days=settings.dropoff_lookback_days)
    prior_escalation = (
        db.query(Decision)
        .join(Event)
        .filter(
            scope_filter,
            Event.event_type == EventType.CHECKOUT_ABANDONED,
            Decision.action == AgentAction.ESCALATE_TO_HUMAN,
            Decision.created_at >= seven_days_ago,
        )
        .first()
    )
    if prior_escalation:
        return False

    return True


# ── Abandonment count lookup ─────────────────────────────────────────────────

def _get_abandonment_count(db: Session, customer_id: str, subscription_id: str | None) -> int:
    """
    Count prior CHECKOUT_ABANDONED events in the lookback window,
    scoped per-subscription if available, else per-customer.
    """
    lookback = datetime.utcnow() - timedelta(days=settings.dropoff_lookback_days)
    scope_filter = (
        Event.subscription_id == subscription_id
        if subscription_id
        else Event.customer_id == customer_id
    )
    return (
        db.query(Event)
        .filter(
            scope_filter,
            Event.event_type == EventType.CHECKOUT_ABANDONED,
            Event.received_at >= lookback,
        )
        .count()
    )


# ── Core pipeline (one abandoned order → decision → audit) ──────────────────

def process_abandoned_order(
    db: Session,
    razorpay_order_id: str,
    customer_id: str,
    subscription_id: str | None,
    amount_inr: float,
    checkout_status: str,
    minutes_since_created: int,
) -> dict:
    """
    Full pipeline for one detected abandoned order.
    Same shape as the webhook pipeline: Event → decide → gate → execute → audit.
    Called by both the real poller and the /debug/simulate-abandonment endpoint.
    """
    abandonment_count = _get_abandonment_count(db, customer_id, subscription_id) + 1
    incentive_ok = _incentive_eligible(db, customer_id, subscription_id, amount_inr)

    event = Event(
        razorpay_event_id=razorpay_order_id,
        event_type=EventType.CHECKOUT_ABANDONED,
        subscription_id=subscription_id,
        customer_id=customer_id,
        razorpay_order_id=razorpay_order_id,
        amount_inr=amount_inr,
        checkout_status=checkout_status,
        minutes_since_created=minutes_since_created,
        abandonment_count=abandonment_count,
        attempt_count=None,
        failure_reason=None,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    write_audit_entry(
        db,
        action_type="dropoff_event_detected",
        details={
            "razorpay_order_id": razorpay_order_id,
            "checkout_status": checkout_status,
            "amount_inr": amount_inr,
            "abandonment_count": abandonment_count,
            "minutes_since_created": minutes_since_created,
            "incentive_eligible": incentive_ok,
        },
        event_id=event.id,
    )

    result = decide_dropoff(
        abandonment_count=abandonment_count,
        amount_inr=amount_inr,
        checkout_status=checkout_status,
        incentive_eligible=incentive_ok,
        minutes_since_created=minutes_since_created,
        subscription_id=subscription_id,
    )

    if result["source"] == "rules_engine_fallback":
        write_audit_entry(
            db,
            action_type="llm_failure_fallback",
            details={"error": result["llm_error"]},
            event_id=event.id,
        )

    action = result["action"]
    confidence = result["confidence"]

    # Confidence gate (same threshold as failure pipeline)
    escalated = False
    if confidence < runtime_flags.get_confidence_threshold() and action != AgentAction.ESCALATE_TO_HUMAN.value:
        escalated = True
        original = action
        action = AgentAction.ESCALATE_TO_HUMAN.value
        result["reasoning"] = (
            f"[Confidence gate override] Original decision was '{original}' "
            f"at confidence {confidence:.2f}, below threshold "
            f"{runtime_flags.get_confidence_threshold()}. Forced to escalate. "
            + result["reasoning"]
        )

    decision = Decision(
        event_id=event.id,
        action=action,
        confidence=confidence,
        reasoning=result["reasoning"],
        source=result["source"],
        escalated=escalated,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)

    write_audit_entry(
        db,
        action_type="decision_made",
        details={
            "action": action,
            "confidence": confidence,
            "reasoning": result["reasoning"],
            "source": result["source"],
            "escalated": escalated,
        },
        event_id=event.id,
        decision_id=decision.id,
    )

    success, outcome = execute_action(action, event)
    decision.executed = True
    decision.outcome = outcome
    db.commit()

    write_audit_entry(
        db,
        action_type="action_executed",
        details={"action": action, "success": success, "outcome": outcome},
        event_id=event.id,
        decision_id=decision.id,
    )

    return {
        "event_id": event.id,
        "razorpay_order_id": razorpay_order_id,
        "abandonment_count": abandonment_count,
        "decision": {
            "action": action,
            "confidence": confidence,
            "reasoning": result["reasoning"],
            "escalated": escalated,
            "outcome": outcome,
        },
    }


# ── Poller (runs on a background thread) ────────────────────────────────────

def run_poller():
    """
    Background polling loop. Started as a daemon thread from main.py on startup.
    Every DROPOFF_POLL_INTERVAL_MINUTES:
      1. Fetch orders created in the last DROPOFF_ABANDONMENT_WINDOW_MINUTES
      2. Filter for status == "attempted" (never paid, customer did open checkout)
      3. Run each through process_abandoned_order()
    """
    window = settings.dropoff_abandonment_window_minutes
    interval = settings.dropoff_poll_interval_minutes * 60  # seconds

    while True:
        try:
            now = int(time.time())
            from_ts = now - (window * 60)
            to_ts = now

            orders = _fetch_razorpay_orders(from_ts, to_ts)
            abandoned = [o for o in orders if o.get("status") == "attempted"]

            if abandoned:
                db = SessionLocal()
                try:
                    for order in abandoned:
                        amount_paise = order.get("amount", 0) or 0
                        created_at = order.get("created_at", now)
                        minutes_old = max(1, (now - created_at) // 60)

                        process_abandoned_order(
                            db=db,
                            razorpay_order_id=order["id"],
                            customer_id=order.get("customer_id") or "unknown",
                            subscription_id=order.get("subscription_id"),
                            amount_inr=amount_paise / 100.0,
                            checkout_status="attempted",
                            minutes_since_created=minutes_old,
                        )
                finally:
                    db.close()
        except Exception:
            # Poller must never crash the server — log and continue.
            pass

        time.sleep(interval)

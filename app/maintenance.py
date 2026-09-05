"""
Periodic background maintenance. Runs as a daemon thread started in
app/main.py, same pattern as the dropoff poller.

Two jobs, both fixing things that used to only ever happen as a side
effect of a customer doing something:

1. TIER REFRESH. refresh_tier() previously only ran when an order resolved
   or a cart event fired. That was fine when a tier was a pure function of
   order counts, but the engagement score has time-based components -
   recency decays, and the behaviour window rolls forward. A customer who
   simply stops shopping should slide down over time; without a sweep,
   nothing recomputes them and the dashboard shows a tier they earned
   months ago. The tier only changes when it genuinely differs, and each
   change still goes through refresh_tier(), so every transition lands in
   the audit trail exactly like a customer-triggered one.

2. STALE ORDER CLEANUP. /checkout creates an Order in CREATED status
   before the customer has paid. If they never complete, that row sits in
   CREATED - a status meaning "still in progress" - forever. Every
   aggregation in the codebase already filters to CAPTURED/FAILED so no
   number is wrong today, but it's a landmine: the first query that
   forgets that filter is silently wrong, and it's hard to spot. Moving
   them to an explicit ABANDONED status makes the state queryable and
   unambiguous instead of accumulating quietly.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from app.audit import write_audit_entry
from app.config import settings
from app.database import SessionLocal
from app.models import (
    Customer, Order, OrderStatus, AgentAction,
    CartEvent, CartEventStatus, CartEventType, PendingSignal, PendingSignalKind,
)
from app.revenue import adjust as adjust_revenue, resolve_cart_loss
from app.tiering import refresh_tier

logger = logging.getLogger(__name__)


def refresh_all_tiers(db) -> dict:
    """Recompute every customer's tier. Returns a summary; each actual
    change is audited by refresh_tier() itself."""
    customers = db.query(Customer).all()
    changed = 0
    for c in customers:
        before = c.tier
        refresh_tier(db, c)
        if c.tier != before:
            changed += 1
    return {"total_customers": len(customers), "changed": changed}


def abandon_stale_orders(db) -> dict:
    """Move CREATED orders older than the configured cutoff to ABANDONED.

    Deliberately never touches CAPTURED or FAILED - this only resolves the
    genuinely ambiguous ones, and only after long enough that no real
    payment could still land on them.
    """
    cutoff = datetime.utcnow() - timedelta(hours=settings.stale_order_abandon_after_hours)
    stale = db.query(Order).filter(
        Order.status == OrderStatus.CREATED,
        Order.created_at < cutoff,
    ).all()
    for order in stale:
        order.status = OrderStatus.ABANDONED
        order.resolved_at = datetime.utcnow()
    if stale:
        db.commit()
    return {"abandoned": len(stale)}


def close_dead_recovery_paths(db) -> dict:
    """Third sweep job: recovery paths that lapse while the customer never
    comes back. The lazy expiry in storefront.py only fires when the
    customer loads the cart page or hits /checkout - a customer who simply
    never returns would otherwise leave their offer PENDING and their
    cart's value "at risk" forever, with total_lost never moving.

    Two shapes, matching how the two nudge kinds are tracked:
    1. PENDING offer cards past expires_at -> EXPIRED + loss booked.
    2. Reminder-only silent abandons (tracked via an invisible
       timeout-attribution signal, no card): signal past expires_at and
       never consumed -> consume it + loss booked. Consuming is what makes
       this exactly-once, same guard /checkout itself uses.
    """
    now = datetime.utcnow()
    stale_cards = db.query(CartEvent).filter(
        CartEvent.status == CartEventStatus.PENDING,
        CartEvent.expires_at.isnot(None),
        CartEvent.expires_at < now,
    ).all()
    for ce in stale_cards:
        ce.status = CartEventStatus.EXPIRED
        ce.resolved_at = now
        db.commit()
        resolve_cart_loss(db, ce, "offer_expired")

    # 3. Failed-payment runs nobody resolved: a FAILED order still
    #    carrying its at-risk booking (risk_settled False), old enough
    #    that no retry is plausibly coming. Same expiry window as every
    #    other nudge/attribution lifetime. Without this, a customer who
    #    walked away after a failed payment left their amount "at risk"
    #    forever and total_lost never learned about it.
    lapsed_runs = db.query(Order).filter(
        Order.status == OrderStatus.FAILED,
        Order.risk_settled.is_(False),
        Order.resolved_at.isnot(None),
        Order.resolved_at < now - timedelta(hours=settings.nudge_expiry_hours),
    ).all()
    for failed_order in lapsed_runs:
        failed_order.risk_settled = True
        db.commit()
        adjust_revenue(db, "at_risk_failed", -failed_order.amount_inr,
                       reason="cart_lost:payment_failure_lapsed", order_id=failed_order.razorpay_order_id)
        adjust_revenue(db, "total_lost", failed_order.amount_inr,
                       reason="cart_lost:payment_failure_lapsed", order_id=failed_order.razorpay_order_id)
        write_audit_entry(
            db, action_type="cart_recovery_lost",
            details={"razorpay_order_id": failed_order.razorpay_order_id,
                      "amount_inr": failed_order.amount_inr, "reason": "payment_failure_lapsed"},
        )

    lapsed_signals = db.query(PendingSignal).filter(
        PendingSignal.kind == PendingSignalKind.TIMEOUT_ATTRIBUTION,
        PendingSignal.consumed_at.is_(None),
        PendingSignal.expires_at < now,
    ).all()
    for sig in lapsed_signals:
        sig.consumed_at = now
        db.commit()
        ce = db.query(CartEvent).filter(CartEvent.id == sig.cart_event_id).first()
        if ce:
            resolve_cart_loss(db, ce, "nudge_window_lapsed")

    # 4. Escalated explicit cancels: the repeat-cancel ladder hands these
    #    to a human and deliberately offers no resume card - which also
    #    meant their declined-bucket booking had NO exit path at all.
    #    Once the window passes with nothing bought, the human review
    #    evidently didn't convert either: book the loss. status=EXPIRED
    #    doubles as the done-marker (these events start with status NULL).
    lapsed_escalations = db.query(CartEvent).filter(
        CartEvent.event_type == CartEventType.EXPLICIT_CANCEL,
        CartEvent.action == AgentAction.ESCALATE_TO_HUMAN,
        CartEvent.status.is_(None),
        CartEvent.created_at < now - timedelta(hours=settings.nudge_expiry_hours),
    ).all()
    for ce in lapsed_escalations:
        ce.status = CartEventStatus.EXPIRED
        ce.resolved_at = now
        db.commit()
        resolve_cart_loss(db, ce, "escalated_cancel_lapsed")

    return {"offers_expired": len(stale_cards), "nudges_lapsed": len(lapsed_signals),
            "failed_runs_lapsed": len(lapsed_runs), "escalations_lapsed": len(lapsed_escalations)}


def run_sweep_once() -> dict:
    db = SessionLocal()
    try:
        orders_result = abandon_stale_orders(db)
        losses_result = close_dead_recovery_paths(db)
        tier_result = refresh_all_tiers(db)
        summary = {**tier_result, **orders_result, **losses_result}
        # Only worth an audit entry when it actually did something -
        # otherwise a sweep every 30 minutes would bury the log in noise.
        if any(losses_result.values()) or tier_result["changed"] or orders_result["abandoned"]:
            write_audit_entry(
                db,
                action_type="maintenance_sweep_completed",
                details=summary,
            )
        return summary
    finally:
        db.close()


def run_sweep() -> None:
    """Daemon-thread entrypoint. Never lets an exception kill the thread -
    a failed sweep should mean 'stale for one more interval', not 'stale
    forever with no sweep running and nothing saying so'."""
    interval_seconds = max(60, settings.tier_refresh_interval_minutes * 60)
    while True:
        try:
            result = run_sweep_once()
            if result["changed"] or result["abandoned"] or result["offers_expired"] or \
                    result["nudges_lapsed"] or result["failed_runs_lapsed"] or result["escalations_lapsed"]:
                logger.info("maintenance sweep: %s", result)
        except Exception:
            logger.exception("maintenance sweep failed; will retry next interval")
        time.sleep(interval_seconds)


def start_sweep_thread() -> threading.Thread:
    thread = threading.Thread(target=run_sweep, daemon=True, name="maintenance-sweep")
    thread.start()
    return thread

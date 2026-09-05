"""
Single-row merchant revenue ledger. Every mutation here also writes to the
existing hash-chained audit log, so the on-screen revenue numbers can be
reconciled against the ledger rather than trusted at face value.
"""
import json

from sqlalchemy.orm import Session

from app.models import MerchantRevenueState, Order, OrderStatus
from app.audit import write_audit_entry

FIELDS = ("total_revenue", "at_risk_soft", "at_risk_declined", "at_risk_failed", "total_recovered", "total_lost", "incentive_cost")


def get_or_create_state(db: Session) -> MerchantRevenueState:
    state = db.query(MerchantRevenueState).filter(MerchantRevenueState.id == 1).first()
    if not state:
        state = MerchantRevenueState(id=1)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def adjust(db: Session, field: str, delta: float, reason: str, order_id: str | None = None) -> MerchantRevenueState:
    if field not in FIELDS:
        raise ValueError(f"Unknown revenue field: {field}")

    state = get_or_create_state(db)
    before = getattr(state, field)
    after = max(0.0, before + delta)   # never let a bucket go negative from a rounding/ordering quirk
    setattr(state, field, after)
    db.commit()
    db.refresh(state)

    write_audit_entry(
        db,
        action_type="revenue_state_updated",
        details={
            "field": field,
            "delta": delta,
            "before": before,
            "after": after,
            "reason": reason,
            "order_id": order_id,
        },
    )
    return state


def as_dict(state: MerchantRevenueState) -> dict:
    total_at_risk = state.at_risk_soft + state.at_risk_declined + state.at_risk_failed
    return {
        "total_revenue": round(state.total_revenue, 2),
        "at_risk_soft": round(state.at_risk_soft, 2),
        "at_risk_declined": round(state.at_risk_declined, 2),
        "at_risk_failed": round(state.at_risk_failed, 2),
        "total_at_risk": round(total_at_risk, 2),
        "total_recovered": round(state.total_recovered, 2),
        "total_lost": round(state.total_lost, 2),
        "incentive_cost": round(state.incentive_cost, 2),
        # Recovered minus what it cost to win it back - the more honest
        # headline number for the scorecard than raw total_recovered alone.
        "net_recovered": round(state.total_recovered - state.incentive_cost, 2),
        "updated_at": state.updated_at,
    }


def round_to_paise_safe(amount_inr: float) -> float:
    """Round to 2 decimal places so amount*100 is always a clean integer
    paise value before it's sent anywhere near Razorpay."""
    return round(amount_inr, 2)


def record_capture_revenue(db: Session, order: Order, was_recovery: bool) -> bool:
    """Book the ledger effects of a captured storefront order EXACTLY once,
    regardless of which confirmation path gets here first.

    Two paths can confirm the same capture: /checkout/verify (the customer's
    own browser callback - HMAC-verified with the key secret, so it can't be
    forged by a client - and almost always seconds ahead) and the
    payment.captured webhook (server-to-server, the fallback for captures
    the browser never reported). They used to share order.status as their
    only guard, which conflated "state transition done" with "revenue
    booked": verify flipped the status without booking revenue, and the
    webhook then saw CAPTURED and skipped booking too - so for any order
    verify confirmed first (i.e. nearly all of them), revenue was never
    booked at all. The revenue_recorded flag separates the two concerns:
    whoever claims it atomically does ALL the money accounting; everyone
    else no-ops.

    Returns True if this call did the booking, False if it was already done.
    """
    claimed = (
        db.query(Order)
        .filter(Order.id == order.id, Order.revenue_recorded.is_(False))
        .update({"revenue_recorded": True}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        return False
    db.refresh(order)

    adjust(db, "total_revenue", order.amount_inr, reason="payment_captured", order_id=order.razorpay_order_id)

    # Settle any failed-payment run this capture resolves. Two shapes of
    # carrier (a FAILED order whose risk_settled flag is still False, i.e.
    # it booked at-risk money nothing has settled yet):
    #  a) this very order, when it transitioned FAILED -> CAPTURED (a
    #     Razorpay-modal retry on the same order id) - was_recovery;
    #  b) an EARLIER failed order for the same basket, when the customer
    #     retried through a fresh /checkout (which mints a new order id).
    #     This was the missing case: only (a) ever released at-risk, so a
    #     successful retry via a new order left the old run's money
    #     stranded in at_risk_soft forever.
    from app.storefront import _items_match  # call-time import: storefront imports this module at load

    carriers = []
    if was_recovery and not order.risk_settled:
        carriers.append(order)
    try:
        captured_items = json.loads(order.items_json)
    except (TypeError, json.JSONDecodeError):
        captured_items = None
    if captured_items is not None:
        open_failed = db.query(Order).filter(
            Order.customer_id == order.customer_id,
            Order.status == OrderStatus.FAILED,
            Order.risk_settled.is_(False),
            Order.id != order.id,
        ).all()
        for candidate in open_failed:
            try:
                if _items_match(json.loads(candidate.items_json), captured_items):
                    carriers.append(candidate)
            except (TypeError, json.JSONDecodeError):
                continue

    for carrier in carriers:
        adjust(db, "at_risk_failed", -carrier.amount_inr, reason="payment_recovered", order_id=order.razorpay_order_id)
        carrier.risk_settled = True
    if carriers:
        db.commit()
        # Recovered books the amount actually collected, once - the risk
        # release above already used the carrier's own booked amount.
        adjust(db, "total_recovered", order.amount_inr, reason="payment_recovered", order_id=order.razorpay_order_id)
        write_audit_entry(
            db, action_type="payment_failure_recovered",
            details={"razorpay_order_id": order.razorpay_order_id, "amount_inr": order.amount_inr,
                      "settled_carriers": [c.razorpay_order_id for c in carriers]},
        )

    if order.recovered_from_cart_event_id:
        # This order exists because of a cart-event nudge - close out the
        # originating at-risk bucket (unless was_recovery - see
        # resolve_cart_recovery's docstring: a same-order retry already had
        # its origin released when it first failed) and book any discount
        # as an explicit incentive_cost line.
        resolve_cart_recovery(db, order, was_recovery)
        write_audit_entry(
            db, action_type="cart_recovery_confirmed",
            details={"razorpay_order_id": order.razorpay_order_id,
                      "recovered_from_cart_event_id": order.recovered_from_cart_event_id,
                      "amount_inr": order.amount_inr, "was_recovery": was_recovery},
        )
    return True


def resolve_cart_to_failed_thread(db: Session, order) -> None:
    """Called once, at the moment a checkout attempt carrying
    recovered_from_cart_event_id first FAILS at the gateway (the run's
    carrier - see webhook._open_run_carrier; retries within a run never
    get re-attributed, so only the carrier-creating failure ever reaches
    here). The missing third exit alongside resolve_cart_recovery
    (captures) and resolve_cart_loss (declined/expired/superseded): the
    origin cart-thread money's fate has moved to the failed-payment
    thread, which has its own exits (retry capture -> recovered,
    give-up/lapse -> lost). Without this, a failed attempt created a
    SECOND booking in at_risk_failed while the original at_risk_soft/
    declined entry for the same money sat there untouched forever.

    Releases the origin's full booked amount; if that's more than what
    this attempt actually tried to charge (a discount applied, or the
    cart shrank before checkout), the shortfall books straight to lost -
    same reconciliation used when explicit-cancel consolidation releases
    a shrunk cart (see storefront._handle_cart_event).
    """
    from app.models import CartEvent, CartEventType  # local import: avoid circular import

    cart_event = db.query(CartEvent).filter(CartEvent.id == order.recovered_from_cart_event_id).first()
    if not cart_event:
        return

    bucket = "at_risk_declined" if cart_event.event_type == CartEventType.EXPLICIT_CANCEL else "at_risk_soft"
    adjust(db, bucket, -cart_event.amount_inr, reason="cart_moved_to_failed_payment", order_id=order.razorpay_order_id)

    shortfall = round(cart_event.amount_inr - order.amount_inr, 2)
    if shortfall > 0:
        adjust(db, "total_lost", shortfall, reason="cart_lost:discount_or_shrink_before_failed_attempt",
               order_id=order.razorpay_order_id)
        write_audit_entry(
            db, action_type="cart_recovery_lost",
            details={"cart_event_id": cart_event.id, "amount_inr": shortfall,
                      "event_type": cart_event.event_type.value, "reason": "discount_or_shrink_before_failed_attempt"},
        )

    write_audit_entry(
        db, action_type="cart_origin_moved_to_failed_thread",
        details={"cart_event_id": cart_event.id, "razorpay_order_id": order.razorpay_order_id,
                  "origin_amount_inr": cart_event.amount_inr, "failed_attempt_amount_inr": order.amount_inr},
    )


def resolve_cart_loss(db: Session, cart_event, reason: str) -> None:
    """Book a cart event's at-risk amount as genuinely lost - its recovery
    path just closed (offer declined/expired/superseded, or none was ever
    opened). Mirror image of resolve_cart_recovery: every rupee that
    enters an at-risk bucket must eventually leave through exactly one of
    the two. Before this existed, nothing anywhere booked into total_lost,
    so dead carts sat "at risk" forever and the dashboard's recovery rate
    - recovered / (recovered + lost) - could only ever read 100%.

    Exactly-once is inherited from the callers' own guarded transitions:
    a PENDING card resolves only once, an attribution signal is consumed
    only once, and the no-recovery-path case runs at event creation only.
    """
    from app.models import CartEventType  # local import: avoid circular import
    bucket = "at_risk_declined" if cart_event.event_type == CartEventType.EXPLICIT_CANCEL else "at_risk_soft"
    adjust(db, bucket, -cart_event.amount_inr, reason=f"cart_lost:{reason}", order_id=None)
    adjust(db, "total_lost", cart_event.amount_inr, reason=f"cart_lost:{reason}", order_id=None)
    write_audit_entry(
        db, action_type="cart_recovery_lost",
        details={"cart_event_id": cart_event.id, "amount_inr": cart_event.amount_inr,
                  "event_type": cart_event.event_type.value, "reason": reason},
    )


def resolve_cart_recovery(db: Session, order, was_recovery: bool = False) -> None:
    """Called once, right after an Order captures, if it carries
    recovered_from_cart_event_id.

    was_recovery=True means this exact order previously went through a
    FAILED state before capturing (a same-order Razorpay-modal retry). In
    that case resolve_cart_to_failed_thread already released the origin's
    at-risk bucket at the moment it first failed, and record_capture_revenue's
    carriers block already booked total_recovered for this payment once -
    doing either again here would double them. Only the incentive-cost
    side (a real discount actually collected) can still be open, so that's
    the only thing that runs in that case.

    Otherwise (the normal path - this capture is the very first resolution
    of the origin event): closes out the originating CartEvent's at-risk
    bucket and books the captured amount as recovered, same as always."""
    from app.models import CartEvent, CartEventType  # local import: avoid circular import

    cart_event = db.query(CartEvent).filter(CartEvent.id == order.recovered_from_cart_event_id).first()
    if not cart_event:
        return

    if not was_recovery:
        bucket = "at_risk_declined" if cart_event.event_type == CartEventType.EXPLICIT_CANCEL else "at_risk_soft"
        adjust(db, bucket, -cart_event.amount_inr, reason="cart_recovered", order_id=order.razorpay_order_id)
        adjust(db, "total_recovered", order.amount_inr, reason="cart_recovered", order_id=order.razorpay_order_id)

    _book_incentive_cost_if_redeemed(db, order, cart_event)


def _book_incentive_cost_if_redeemed(db: Session, order, cart_event) -> None:
    """Cost books ONLY for a redeemed offer (status RESUMED - the one path
    that sets incentive_final_amount_inr to the terms actually charged).
    A DECLINED offer keeps its proposal-time figure, but a recovery
    attributed to it via the reminder signal was paid at FULL price -
    without the status gate that stale proposal would book phantom cost.
    Runs regardless of was_recovery: a discount genuinely collected on an
    eventual capture is real cost even if the same order failed once first."""
    from app.models import CartEventStatus  # local import: avoid circular import

    if (cart_event.status == CartEventStatus.RESUMED
            and cart_event.incentive_final_amount_inr is not None and cart_event.incentive_pct):
        # The discount floats with an edited cart now, so the true giveaway
        # must be derived from the charge actually made and the stored %,
        # NOT from the offer-time cart amount (which may describe a
        # different basket entirely): full = paid / (1 - pct/100), so
        # cost = full - paid = paid * pct / (100 - pct).
        pct = cart_event.incentive_pct
        if pct < 100:
            cost = round(order.amount_inr * pct / (100.0 - pct), 2)
            if cost > 0:
                adjust(db, "incentive_cost", cost, reason="incentive_redeemed", order_id=order.razorpay_order_id)

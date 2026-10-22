"""
Failed storefront orders: a real Razorpay Payment Link for the exact basket
that failed, plus at most ONE reminder on the day the customer named.

Deliberately NOT a promise (decided 2026-10-20): a shopper whose card failed
owes nothing - nothing shipped. Kept/broken status, escalation, broken-promise
caps and memory notes all assume a debt and would treat a hesitant shopper
like a defaulter. Recovery attribution needs none of it: any capture of the
same basket already releases the failed-payment run (revenue.record_capture_revenue).

Offers (decided 2026-10-20): if the cart ALREADY had an offer when the
payment failed, the failed order's amount already IS the discounted price
(/checkout priced it from the offer). The message says so - "% off, ₹X
instead of ₹Y, valid till ..." - and the link expires no later than the
offer. No new offer is ever minted for a card failure (it would give money
away on a purchase already decided, and burn the per-30-day allowance). Once
the offer has ended the customer is sent back to the store instead of a
full-price link: the failed run is booked at the discounted amount, and the
store's own checkout prices and books a new attempt correctly.

Money: link paid -> the carrier order is CAPTURED through the same
revenue.record_capture_revenue a store checkout uses (exactly-once,
at_risk_failed -> recovered, incentive_cost), so payment.captured for that
payment must NOT book again (webhook.py checks classify_payment). A basket
paid in the store cancels the link; if both still capture, the second payment
is flagged `duplicate_basket_payment` for a merchant refund - never booked
twice, never refunded automatically.
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import clock, contacts, messaging, payment_links
from app.audit import write_audit_entry
from app.models import (
    CartEvent, CartEventStatus, Customer, Order, OrderStatus, OutboundMessage, PaymentLink,
    ScheduledAction, ScheduledKind, ScheduledStatus,
)
from app.scheduler import register_handler, schedule

logger = logging.getLogger(__name__)

SUBJECT = "order"
# A failed-basket link lives at most this long even without an offer: prices
# and stock move, and an old link should not charge yesterday's basket.
MAX_LINK_LIFE = timedelta(hours=72)
_IST = timedelta(hours=5, minutes=30)


def _until(dt: datetime) -> str:
    return (dt + _IST).strftime("%d %b, %I:%M %p").replace(" 0", " ")


def offer_snapshot(db: Session, order: Order) -> dict | None:
    """The offer this basket was ALREADY priced with, or None. Only a RESUMED
    offer whose redeemed amount is exactly what this order tried to charge
    counts (over the tier cap the discount was suspended - no offer to show)."""
    if not order.recovered_from_cart_event_id:
        return None
    ce = db.query(CartEvent).filter(CartEvent.id == order.recovered_from_cart_event_id).first()
    if (ce is None or ce.incentive_pct is None or ce.status != CartEventStatus.RESUMED
            or ce.incentive_final_amount_inr is None
            or abs(ce.incentive_final_amount_inr - order.amount_inr) > 0.01):
        return None
    from app.storefront import _cart_amount   # local: storefront imports revenue -> this module
    try:
        original = round(_cart_amount(json.loads(order.items_json)), 2)
    except (TypeError, ValueError, KeyError):
        original = None
    if not original or original <= order.amount_inr:
        original = round(order.amount_inr / (1 - ce.incentive_pct / 100), 2)
    return {"pct": ce.incentive_pct, "original_amount_inr": original, "ends_at": ce.expires_at}


def _customer_bits(db: Session, order: Order):
    customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
    cp = contacts.for_customer(db, order.customer_id)
    lang = cp.preferred_language if cp and cp.preferred_language else contacts.DEFAULT_LANGUAGE
    return customer, cp, lang


def send_order_link(db: Session, order: Order, reason: str = "call") -> OutboundMessage:
    """The message for a failed order (a call's PAY_NOW / PROMISE, or the reminder)."""
    customer, cp, lang = _customer_bits(db, order)
    name = customer.name if customer else "Customer"
    phone = cp.phone if cp else None
    offer = offer_snapshot(db, order)
    now = datetime.utcnow()

    if offer and offer["ends_at"] and offer["ends_at"] <= now + timedelta(minutes=20):
        # The offer is over: no link at the old discounted price, and no
        # full-price link either (see module docstring) - back to the store.
        payment_links.close_for_subject(db, SUBJECT, order.id, reason="offer_ended")
        return messaging.send_message(
            db, "order_offer_ended", subject_type=SUBJECT, subject_id=order.id, phone=phone, language=lang,
            variables={"name": name, "pct": f"{offer['pct']:g}", "until": _until(offer["ends_at"])},
            link="/store", link_kind="storefront")

    if not phone:
        # Nobody to send it to: do not spend a Test Mode link slot.
        return messaging.send_message(
            db, "payment_link", subject_type=SUBJECT, subject_id=order.id, phone=None, language=lang,
            variables={"name": name, "amount": order.amount_inr}, link=None, link_kind=None)

    link = payment_links.ensure_link(
        db, SUBJECT, order.id, order.amount_inr, expire_at=now + MAX_LINK_LIFE,
        description=f"{messaging.MERCHANT_NAME} order", offer=offer,
        customer={"name": name, "email": getattr(customer, "email", None), "contact": phone})
    kind = "placeholder" if link.is_stub else "razorpay_payment_link"
    if offer:
        return messaging.send_message(
            db, "payment_link_offer", subject_type=SUBJECT, subject_id=order.id, phone=phone, language=lang,
            variables={"name": name, "pct": f"{offer['pct']:g}",
                       "original": messaging._inr(offer["original_amount_inr"]),
                       "amount": order.amount_inr, "until": _until(link.expire_at)},
            link=link.short_url, link_kind=kind)
    return messaging.send_message(
        db, "payment_link", subject_type=SUBJECT, subject_id=order.id, phone=phone, language=lang,
        variables={"name": name, "amount": order.amount_inr}, link=link.short_url, link_kind=kind)


# ── The one reminder ─────────────────────────────────────────────────────────

def schedule_reminder(db: Session, order: Order, on_date) -> ScheduledAction | None:
    """10:00 IST on the day the customer named (a contact kind, so the
    scheduler also holds it to 08:00-19:00). One per order: a newer date
    replaces the older reminder."""
    from app.scheduler import cancel_for_subject
    due = datetime(on_date.year, on_date.month, on_date.day, 10, 0) - _IST
    if due <= clock.now():
        return None
    cancel_for_subject(db, SUBJECT, order.id, reason="reminder_replaced",
                       kinds=[ScheduledKind.ORDER_LINK_REMINDER])
    return schedule(db, ScheduledKind.ORDER_LINK_REMINDER, SUBJECT, order.id, due,
                    payload={"named_date": on_date.isoformat()}, customer_ref=order.customer_id)


def _handle_reminder(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    order = db.query(Order).filter(Order.id == action.subject_id).first()
    if order is None or order.status != OrderStatus.FAILED or order.risk_settled:
        return ScheduledStatus.SKIPPED.value, "order_already_resolved"
    msg = send_order_link(db, order, reason="reminder")
    sent = messaging.delivered(msg)
    write_audit_entry(db, "order_link_reminder_sent" if sent else "order_link_reminder_not_sent", {
        "order_id": order.id, "message_id": msg.id, "message_status": msg.status,
        "template": msg.template})
    return ScheduledStatus.DONE.value, ("order_link_reminder_sent" if sent
                                        else f"order_link_reminder_not_sent:{msg.status}")


register_handler(ScheduledKind.ORDER_LINK_REMINDER, _handle_reminder)


# ── Link paid ────────────────────────────────────────────────────────────────

def _on_link_payment(db: Session, link: PaymentLink, amount_inr: float, payment_id: str | None) -> dict:
    from app.revenue import record_capture_revenue
    from app.tiering import refresh_tier
    order = db.query(Order).filter(Order.id == link.subject_id).with_for_update().first()
    if order is None:
        db.commit()
        return {"order": "unknown"}
    if order.status != OrderStatus.FAILED or order.risk_settled or order.revenue_recorded:
        # Paid in the store (or by another link) first: this money is extra.
        db.commit()
        write_audit_entry(db, "duplicate_basket_payment", {
            "order_id": order.id, "payment_link_row": link.id, "payment_id": payment_id,
            "amount_inr": amount_inr, "order_status": order.status.value,
            "note": "not booked; refund from the dashboard"})
        return {"order": order.id, "duplicate": True}
    order.status = OrderStatus.CAPTURED
    order.razorpay_payment_id = payment_id
    order.resolved_at = datetime.utcnow()
    db.commit()
    booked = record_capture_revenue(db, order, was_recovery=True)
    if order.customer:
        refresh_tier(db, order.customer)
    from app.scheduler import cancel_for_subject
    cancel_for_subject(db, SUBJECT, order.id, reason="paid_by_link")
    write_audit_entry(db, "order_paid_by_link", {
        "order_id": order.id, "payment_link_row": link.id, "payment_id": payment_id,
        "amount_inr": amount_inr, "revenue_booked": booked, "offer_pct": link.offer_pct})
    return {"order": order.id, "recovered": booked}


payment_links.register_link_handler(SUBJECT, _on_link_payment)


def close_links_for_settled_carriers(db: Session, carrier_ids: list[str]) -> None:
    """Called by revenue.record_capture_revenue when a store capture settles
    failed runs: their links must not stay payable (double-pay guard)."""
    for cid in carrier_ids:
        payment_links.close_for_subject(db, SUBJECT, cid, reason="basket_paid_elsewhere")
        from app.scheduler import cancel_for_subject
        cancel_for_subject(db, SUBJECT, cid, reason="basket_paid_elsewhere",
                           kinds=[ScheduledKind.ORDER_LINK_REMINDER])


def duplicate_payments(db: Session) -> list[dict]:
    """Link payments that arrived after the basket was already paid - for the
    dashboard's refund list."""
    out = []
    rows = (db.query(PaymentLink).filter(PaymentLink.subject_type == SUBJECT,
                                         PaymentLink.amount_paid_inr > 0).all())
    for link in rows:
        order = db.query(Order).filter(Order.id == link.subject_id).first()
        refunded = set(json.loads(link.refunded_payment_ids or "[]"))
        for pid in json.loads(link.paid_payment_ids or "[]"):
            if order is not None and pid != order.razorpay_payment_id and pid not in refunded:
                out.append({"payment_link_row": link.id, "order_id": link.subject_id, "payment_id": pid,
                            "amount_inr": link.amount_inr, "paid_at": link.closed_at})
    return out

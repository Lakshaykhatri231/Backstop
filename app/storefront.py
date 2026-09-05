"""
Customer-facing storefront API: register/login, a tiny hardcoded catalog,
a JSON-blob cart, and real Razorpay order creation for checkout.

This is the front door that feeds real, tier-aware events into the existing
recovery pipeline (webhook.py, dropoff.py, rules_engine.py) - it doesn't
duplicate any decision logic itself except for the pre-checkout cart-event
path, which has no equivalent anywhere else in the app.
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import (
    Customer, Order, OrderStatus, CartEvent, CartEventType, CartEventStatus,
    AgentAction, PendingSignal, PendingSignalKind, CustomerTier, Event, EventType, Decision,
)
from app.auth import hash_password, verify_password, create_token, get_current_customer
from app.tiering import (
    refresh_tier, customer_stats, tier_breakdown, incentive_pct_for_customer,
)
from app.rules_engine import rule_based_cart_event_decision
from app.actions import execute_action
from app.audit import write_audit_entry
from app.revenue import adjust as adjust_revenue, round_to_paise_safe, record_capture_revenue, resolve_cart_loss
from app import runtime_flags

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Hardcoded catalog - this project's focus is transactions, not commerce ──
CATALOG = [
    {"id": "sku_001", "name": "Wireless Earbuds",     "price_inr": 1499},
    {"id": "sku_002", "name": "Mechanical Keyboard",  "price_inr": 3999},
    {"id": "sku_003", "name": "Smart Watch",          "price_inr": 5999},
    {"id": "sku_004", "name": "Yoga Mat",             "price_inr": 899},
    {"id": "sku_005", "name": "Espresso Machine",     "price_inr": 8999},
    {"id": "sku_006", "name": "Bluetooth Speaker",    "price_inr": 2499},
]
CATALOG_BY_ID = {p["id"]: p for p in CATALOG}


def _cart(customer: Customer) -> list[dict]:
    return json.loads(customer.cart_json or "[]")


def _cart_amount(cart: list[dict]) -> float:
    return sum(CATALOG_BY_ID[i["sku"]]["price_inr"] * i["qty"] for i in cart if i["sku"] in CATALOG_BY_ID)


def _active_offer(db: Session, customer: Customer, cart_amount: float) -> dict | None:
    """The customer's live discount offer, if any, priced against their
    CURRENT cart - the single source of truth the cart UI renders its dual
    totals from, so what's shown and what /checkout would charge can never
    disagree. The discount follows the cart (no exact-items requirement any
    more) but the tier's amount cap keeps holding: over the cap the offer
    is suspended (within_cap=false, no discounted amount), and it snaps
    back the moment the cart drops under."""
    offer = (
        db.query(CartEvent)
        .filter(
            CartEvent.customer_id == customer.id,
            CartEvent.status == CartEventStatus.PENDING,
            CartEvent.incentive_pct.isnot(None),
        )
        .order_by(CartEvent.created_at.desc())
        .first()
    )
    if offer:
        offer = _expire_stale_cancel_offer(db, offer)
    if not offer or offer.status != CartEventStatus.PENDING:
        return None
    cap = runtime_flags.get_incentive_max_order_value(customer.tier.value)
    within_cap = 0 < cart_amount <= cap
    return {
        "cart_event_id": offer.id,
        "incentive_pct": offer.incentive_pct,
        "amount_cap_inr": cap,
        "within_cap": within_cap,
        "full_amount_inr": cart_amount,
        "discounted_amount_inr": (
            round_to_paise_safe(cart_amount * (1 - offer.incentive_pct / 100)) if within_cap else None
        ),
    }


def _cart_response(db: Session, customer: Customer) -> dict:
    cart = _cart(customer)
    amount = _cart_amount(cart)
    return {"items": cart, "amount_inr": amount, "active_offer": _active_offer(db, customer, amount)}


# ── Auth ──────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(Customer).filter(Customer.email == req.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    customer = Customer(email=req.email, password_hash=hash_password(req.password), name=req.name)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return {"token": create_token(customer.id), "customer": _customer_out(db, customer)}


@router.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.email == req.email).first()
    if not customer or not verify_password(req.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"token": create_token(customer.id), "customer": _customer_out(db, customer)}


@router.get("/auth/me")
def me(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    return _customer_out(db, customer)


def _customer_out(db: Session, customer: Customer) -> dict:
    return {
        "id": customer.id,
        "email": customer.email,
        "name": customer.name,
        "tier": customer.tier.value,
        "stats": customer_stats(db, customer.id),
    }


# ── Catalog + cart ────────────────────────────────────────────────────────

@router.get("/catalog")
def catalog():
    return CATALOG


class CartAddRequest(BaseModel):
    sku: str
    qty: int = 1


@router.get("/cart")
def get_cart(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    return _cart_response(db, customer)


@router.post("/cart/add")
def add_to_cart(req: CartAddRequest, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    if req.sku not in CATALOG_BY_ID:
        raise HTTPException(status_code=404, detail="Unknown SKU")
    cart = _cart(customer)
    for item in cart:
        if item["sku"] == req.sku:
            item["qty"] += req.qty
            break
    else:
        cart.append({"sku": req.sku, "qty": req.qty})
    customer.cart_json = json.dumps(cart)
    customer.cart_updated_at = datetime.utcnow()
    db.commit()
    return _cart_response(db, customer)


@router.post("/cart/remove")
def remove_from_cart(req: CartAddRequest, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    cart = [i for i in _cart(customer) if i["sku"] != req.sku]
    customer.cart_json = json.dumps(cart)
    customer.cart_updated_at = datetime.utcnow()
    db.commit()
    return _cart_response(db, customer)


@router.delete("/cart")
def delete_cart(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    """Explicit cancel: the customer actively deleted their cart before ever
    reaching checkout. This is a real action (not a simulation) - always
    logged as CartEventType.EXPLICIT_CANCEL. See rule_based_cart_event_decision
    for why this is handled more conservatively than a silent timeout.

    Snapshot the cart BEFORE deleting it - if the decision ends up being
    offer_incentive or send_resume_link, that card needs real items to show
    and to resume, and there's nothing left to snapshot once this commits."""
    cart = _cart(customer)
    if not cart:
        return {"status": "cart_already_empty"}
    amount = _cart_amount(cart)
    customer.cart_json = "[]"
    customer.cart_updated_at = datetime.utcnow()
    db.commit()
    result = _handle_cart_event(db, customer, CartEventType.EXPLICIT_CANCEL, cart, amount)
    return {"status": "cart_deleted", "recovery_decision": result}


# ── Checkout: real Razorpay order creation ───────────────────────────────

def _create_razorpay_order(
    db: Session,
    customer: Customer,
    cart: list[dict],
    amount_inr: float,
    recovered_from_cart_event_id: str | None = None,
) -> dict:
    """Shared by the normal /checkout flow and /cart/resume. amount_inr may
    be a discounted amount (resume-with-incentive) - callers are responsible
    for rounding it to a clean paise value first."""
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        raise HTTPException(
            status_code=500,
            detail="RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not configured - add test-mode keys to .env",
        )

    receipt = f"rcpt_{uuid.uuid4().hex[:16]}"
    try:
        resp = httpx.post(
            "https://api.razorpay.com/v1/orders",
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            json={
                "amount": int(round(amount_inr * 100)),  # paise
                "currency": "INR",
                "receipt": receipt,
                "notes": {"customer_id": customer.id, "customer_email": customer.email},
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        rzp_order = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Razorpay order creation failed: {e}")

    order = Order(
        customer_id=customer.id,
        razorpay_order_id=rzp_order["id"],
        items_json=json.dumps(cart),
        amount_inr=amount_inr,
        status=OrderStatus.CREATED,
        recovered_from_cart_event_id=recovered_from_cart_event_id,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    write_audit_entry(
        db,
        action_type="storefront_order_created",
        details={
            "razorpay_order_id": rzp_order["id"], "amount_inr": amount_inr, "customer_id": customer.id,
            "recovered_from_cart_event_id": recovered_from_cart_event_id,
        },
    )

    # Any *other* still-pending cancel-resume card for this customer is now
    # moot - they've placed an order one way or another. Don't touch the
    # card being resumed right now (if any); that's set to RESUMED by the
    # caller instead.
    other_pending = db.query(CartEvent).filter(
        CartEvent.customer_id == customer.id,
        CartEvent.status == CartEventStatus.PENDING,
        CartEvent.id != recovered_from_cart_event_id,
    ).all()
    for ce in other_pending:
        ce.status = CartEventStatus.SUPERSEDED_BY_NEW_ORDER
        ce.resolved_at = datetime.utcnow()
    if other_pending:
        db.commit()
        for ce in other_pending:
            resolve_cart_loss(db, ce, "superseded_by_new_order")

    return {
        "razorpay_order_id": rzp_order["id"],
        "razorpay_key_id": settings.razorpay_key_id,   # public by design, safe to expose
        "amount_paise": rzp_order["amount"],
        "currency": "INR",
        "customer_name": customer.name,
        "customer_email": customer.email,
        "local_order_id": order.id,
    }


def _items_match(a: list[dict], b: list[dict]) -> bool:
    """Order-independent comparison of {sku, qty} lists. A locked-in discount
    only honors the exact cart it was offered against - if the cart has
    changed since, the offer no longer cleanly applies to what's being
    bought and should void rather than silently cover extra items."""
    norm = lambda items: sorted((i["sku"], i["qty"]) for i in items)
    return norm(a) == norm(b)


@router.post("/checkout")
def checkout(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    cart = _cart(customer)
    if not cart:
        raise HTTPException(status_code=400, detail="Cart is empty")

    amount_inr = _cart_amount(cart)
    recovered_from_cart_event_id = None

    # Any still-valid offer (timeout OR cancel-resume) for this customer?
    # A pending offer only ever gets created for events that led to
    # offer_incentive or send_resume_link - see _handle_cart_event.
    pending_offer = (
        db.query(CartEvent)
        .filter(CartEvent.customer_id == customer.id, CartEvent.status == CartEventStatus.PENDING)
        .order_by(CartEvent.created_at.desc())
        .first()
    )
    if pending_offer:
        pending_offer = _expire_stale_cancel_offer(db, pending_offer)

    if pending_offer and pending_offer.status == CartEventStatus.PENDING:
        if pending_offer.incentive_pct is not None:
            # The discount FOLLOWS the cart now instead of demanding an
            # exact item match: the % was approved for this customer, so it
            # applies to whatever they're buying - as long as the tier's
            # amount cap that approved it keeps holding. Over the cap the
            # discount is suspended (the cart page already said so via
            # _active_offer, which uses this exact same rule) and they pay
            # full price - but the purchase still counts as a recovery:
            # attribution and discount are independent questions.
            cap = runtime_flags.get_incentive_max_order_value(customer.tier.value)
            if amount_inr <= cap:
                discounted = round_to_paise_safe(amount_inr * (1 - pending_offer.incentive_pct / 100))
                # Snapshot the terms actually redeemed - the offer-time
                # figure was priced against a cart that may since have
                # changed, and resolve_cart_recovery derives the real
                # incentive cost from this + the stored %.
                pending_offer.incentive_final_amount_inr = discounted
                amount_inr = discounted
            else:
                pending_offer.incentive_final_amount_inr = None  # suspended: no discount given, no incentive_cost to book
            recovered_from_cart_event_id = pending_offer.id
            pending_offer.status = CartEventStatus.RESUMED
            pending_offer.resolved_at = datetime.utcnow()
            db.commit()
        else:
            # No money term attached (plain resume-link) - attribute the
            # recovery regardless of what the cart looks like now.
            recovered_from_cart_event_id = pending_offer.id
            pending_offer.status = CartEventStatus.RESUMED
            pending_offer.resolved_at = datetime.utcnow()
            db.commit()

    # Invisible attribution check: was a timeout nudge (with no incentive,
    # e.g. a plain reminder) shown recently, and is the customer now
    # completing an order? No special click required for this one.
    if recovered_from_cart_event_id is None:
        signal = (
            db.query(PendingSignal)
            .filter(
                PendingSignal.customer_id == customer.id,
                PendingSignal.kind == PendingSignalKind.TIMEOUT_ATTRIBUTION,
                PendingSignal.consumed_at.is_(None),
                PendingSignal.expires_at > datetime.utcnow(),
            )
            .order_by(PendingSignal.created_at.desc())
            .first()
        )
        if signal:
            recovered_from_cart_event_id = signal.cart_event_id
            signal.consumed_at = datetime.utcnow()
            db.commit()

    return _create_razorpay_order(db, customer, cart, amount_inr, recovered_from_cart_event_id)


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/checkout/verify")
def verify_payment(req: VerifyPaymentRequest, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    """
    Server-side verification of the Razorpay Standard Checkout success callback.

    The frontend `handler` receives razorpay_order_id/payment_id/signature and
    MUST NOT be trusted on its own - a modified client could fabricate a
    "success" callback without ever paying. This recomputes the expected
    signature server-side (HMAC-SHA256 of "order_id|payment_id" using the
    account's key secret, per Razorpay's documented scheme) and only treats
    the payment as verified if it matches.

    Revenue: this endpoint also books the capture into the merchant ledger,
    via revenue.record_capture_revenue's exactly-once flag, with the
    payment.captured webhook as the other (fallback) caller. It used to
    deliberately leave revenue to the webhook alone, but that design had a
    fatal interaction with the webhook handler's already-captured check:
    this endpoint nearly always wins the race (it's the customer's own
    browser reporting back; the webhook has delivery latency), so the
    webhook found status == CAPTURED, assumed the money was already booked,
    and skipped it - meaning storefront revenue was never recorded at all.
    Booking here is sound: the callback signature is HMAC-SHA256 over
    "order_id|payment_id" with the account's key secret, which a client
    cannot forge - it carries the same trust as the webhook signature.
    """
    if not (req.razorpay_order_id and req.razorpay_payment_id and req.razorpay_signature):
        raise HTTPException(status_code=400, detail="Missing required fields")

    if not settings.razorpay_key_secret:
        raise HTTPException(status_code=500, detail="RAZORPAY_KEY_SECRET not configured")

    payload = f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode("utf-8")
    expected_signature = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, req.razorpay_signature):
        write_audit_entry(
            db,
            action_type="checkout_signature_verification_failed",
            details={"razorpay_order_id": req.razorpay_order_id, "customer_id": customer.id},
        )
        # Do NOT mark as paid on a mismatch - this is the whole point of verifying.
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    order = db.query(Order).filter(
        Order.razorpay_order_id == req.razorpay_order_id,
        Order.customer_id == customer.id,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="No matching order for this customer")

    already_captured = order.status == OrderStatus.CAPTURED
    revenue_booked = False
    if not already_captured:
        # Must be read BEFORE the transition below overwrites it - a FAILED
        # order re-paid through the same Razorpay order id is a recovery,
        # and its amount has to move out of at-risk, not just into revenue.
        was_recovery = order.status == OrderStatus.FAILED

        order.status = OrderStatus.CAPTURED
        order.razorpay_payment_id = req.razorpay_payment_id
        order.resolved_at = datetime.utcnow()
        customer.cart_json = "[]"
        customer.cart_updated_at = datetime.utcnow()
        db.commit()
        refresh_tier(db, customer)

        write_audit_entry(
            db,
            action_type="checkout_signature_verified",
            details={"razorpay_order_id": req.razorpay_order_id,
                      "razorpay_payment_id": req.razorpay_payment_id,
                      "amount_inr": order.amount_inr},
        )
        # Exactly-once revenue booking - see record_capture_revenue. If the
        # payment.captured webhook somehow got here first, this no-ops.
        revenue_booked = record_capture_revenue(db, order, was_recovery)

    return {"status": "verified", "already_processed": already_captured,
            "revenue_booked": revenue_booked, "order_id": order.id}


@router.post("/checkout/give-up-failed")
def give_up_failed(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    """The customer explicitly walks away from failed payment attempt(s):
    "cancelled the order and moved on". Settles every open failed run
    (at_risk_failed -> total_lost), quietly clears the cart, and consumes
    any pending failure-notice toast. Deliberately does NOT create a cart
    event - the loss belongs to the failed-payment thread; recording an
    explicit cancel too would double-track the same money on two threads.
    Idempotent: a second call finds no open runs and no-ops."""
    open_failed = db.query(Order).filter(
        Order.customer_id == customer.id,
        Order.status == OrderStatus.FAILED,
        Order.risk_settled.is_(False),
    ).all()
    settled = []
    for failed_order in open_failed:
        failed_order.risk_settled = True
        db.commit()
        adjust_revenue(db, "at_risk_failed", -failed_order.amount_inr,
                       reason="cart_lost:payment_failure_given_up", order_id=failed_order.razorpay_order_id)
        adjust_revenue(db, "total_lost", failed_order.amount_inr,
                       reason="cart_lost:payment_failure_given_up", order_id=failed_order.razorpay_order_id)
        settled.append({"razorpay_order_id": failed_order.razorpay_order_id, "amount_inr": failed_order.amount_inr})

    # Surface the give-up on the Event Feed as its OWN new row, not by
    # touching any of the failed payments' existing Event/Decision rows.
    # Each retry already has its own honest record of what the agent
    # decided at the time (retry_now, escalated, ...) - overwriting those
    # outcomes to "customer_gave_up" destroyed that history (a real bug:
    # a customer with two failed attempts saw BOTH prior decisions
    # replaced by the same generic label). One give-up click is one new,
    # separate decision - the customer's, not the agent's - so it gets one
    # new line, and every earlier row stays exactly as it was.
    if settled:
        total_amount = round(sum(r["amount_inr"] for r in settled), 2)
        event = Event(
            event_type=EventType.PAYMENT_FAILURE_GIVEN_UP,
            customer_id=customer.id,
            amount_inr=total_amount,
        )
        db.add(event)
        db.commit()
        db.refresh(event)

        decision = Decision(
            event_id=event.id,
            action=AgentAction.NO_ACTION,
            confidence=1.0,
            reasoning=(
                f"Customer explicitly gave up on {len(settled)} failed payment attempt(s) "
                f"totaling ₹{total_amount:.0f} and moved on. Settled: "
                + ", ".join(r["razorpay_order_id"] for r in settled) + "."
            ),
            source="customer_action",
            escalated=False,
            executed=True,
            outcome="customer_gave_up",
        )
        db.add(decision)
        db.commit()
        db.refresh(decision)

        write_audit_entry(
            db, action_type="payment_failure_given_up",
            details={"customer_id": customer.id, "settled_runs": settled, "total_lost_inr": total_amount},
            event_id=event.id, decision_id=decision.id,
        )

    customer.cart_json = "[]"
    customer.cart_updated_at = datetime.utcnow()
    notices = db.query(PendingSignal).filter(
        PendingSignal.customer_id == customer.id,
        PendingSignal.kind == PendingSignalKind.PAYMENT_FAILURE_NOTICE,
        PendingSignal.consumed_at.is_(None),
    ).all()
    for n in notices:
        n.consumed_at = datetime.utcnow()
    db.commit()

    return {"settled_runs": settled, "cart_cleared": True}


# ── Pre-checkout cart events (silent timeout / explicit cancel) ─────────────

def _cart_freq_cap_ok(db: Session, customer_id: str, tier: CustomerTier) -> bool:
    """30-day frequency cap on cart-event incentives specifically - ONLY
    this gate, despite the old name of this function ("incentive_eligible")
    suggesting it covered everything. It didn't: the order-value cap and
    tier gate are separate checks (see _handle_cart_event below), and this
    function's result used to be the only one of the three ever stored
    anywhere, under that misleading name. Separate from (and in addition
    to) tier, which only says 'this kind of customer generally qualifies' -
    tier is recomputed lazily and can lag by one event, so without this a
    trusted customer could otherwise farm a discount every time they hit
    timeout/cancel.

    The cap is now PER TIER, not one number for everyone, and it runs the
    opposite way to the discount bands: loyal customers get the largest
    discount but only one shot at it per 30 days, casual customers get a
    small discount but three. Scaling discount size, frequency and
    cancel-tolerance all upward for the same tier would hand the segment
    with the most room to exploit them all three advantages at once.
    """
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    prior_count = db.query(CartEvent).filter(
        CartEvent.customer_id == customer_id,
        CartEvent.action == AgentAction.OFFER_INCENTIVE,
        CartEvent.created_at >= thirty_days_ago,
    ).count()
    return prior_count < runtime_flags.get_incentive_max_per_30d(tier.value)


def _find_open_duplicate_abandon(db: Session, customer: Customer, cart: list[dict]) -> CartEvent | None:
    """Has this exact cart already been logged as a silent abandon that is
    still open?

    The tutorial's "simulate cart timeout" button can be clicked any number
    of times, and each click used to book the cart's value into the
    merchant's at-risk ledger again - so ten clicks meant ten times the
    money showing as at risk, none of it real. Rate-limiting the button
    would have been the wrong fix (it's a legitimate tutorial control);
    the actual bug is that the same idle cart was being treated as a new
    loss each time.

    This is not demo-only either. Real idle-timeout detection would be a
    scheduled sweep over stale carts, which would re-detect the same cart
    on every pass and hit exactly the same bug. Deduplicating here fixes
    both.

    "Still open" = logged recently enough to be live (nudge expiry window),
    for the same items, with no successful purchase since.
    """
    window_start = datetime.utcnow() - timedelta(hours=runtime_flags.get_nudge_expiry_hours())
    recent = db.query(CartEvent).filter(
        CartEvent.customer_id == customer.id,
        CartEvent.event_type == CartEventType.SILENT_ABANDON,
        CartEvent.created_at >= window_start,
    ).order_by(CartEvent.created_at.desc()).all()

    for prior in recent:
        try:
            prior_items = json.loads(prior.items_json or "[]")
        except json.JSONDecodeError:
            continue
        if not _items_match(prior_items, cart):
            continue
        # A capture since then means that cart genuinely resolved - a new
        # abandon of the same basket afterwards is a real, separate event.
        captured_since = db.query(Order.id).filter(
            Order.customer_id == customer.id,
            Order.status == OrderStatus.CAPTURED,
            Order.created_at >= prior.created_at,
        ).first()
        if captured_since:
            continue
        # "Still open" also has to mean the event was never ANSWERED. A
        # PENDING offer is open; a status-less reminder or a DECLINED
        # offer is open only while its attribution signal is still live
        # (signal consumed/lapsed = its at-risk already exited via
        # recovery or loss, so a fresh abandon is a genuinely new event).
        # Matching an already-resolved event here made a repeat timeout
        # return a decision the customer had answered - and suppressed the
        # new event entirely.
        if prior.status == CartEventStatus.PENDING:
            return prior
        if prior.status in (None, CartEventStatus.DECLINED):
            live_signal = db.query(PendingSignal.id).filter(
                PendingSignal.cart_event_id == prior.id,
                PendingSignal.kind == PendingSignalKind.TIMEOUT_ATTRIBUTION,
                PendingSignal.consumed_at.is_(None),
                PendingSignal.expires_at > datetime.utcnow(),
            ).first()
            if live_signal:
                return prior
        continue
    return None


def _open_failed_run_for_items(db: Session, customer_id: str, cart: list[dict]) -> Order | None:
    """An OPEN failed-payment run (an unretried FAILED order still
    carrying its at_risk_failed booking - see webhook._open_run_carrier,
    the same concept from the opposite direction) already covers this
    exact basket. Guards _handle_cart_event against creating a SECOND,
    independent at-risk booking for money that's already tracked on the
    failed-payment thread - reachable directly through the demo's own
    buttons: re-click 'Simulate cart timeout' or 'Delete cart' on a cart
    whose payment just failed, instead of using Retry or Give up."""
    candidates = db.query(Order).filter(
        Order.customer_id == customer_id,
        Order.status == OrderStatus.FAILED,
        Order.risk_settled.is_(False),
    ).all()
    for candidate in candidates:
        try:
            if _items_match(json.loads(candidate.items_json), cart):
                return candidate
        except (TypeError, json.JSONDecodeError):
            continue
    return None


def _open_created_order_for_items(db: Session, customer_id: str, cart: list[dict]) -> Order | None:
    """An open (status == CREATED) Order matching this exact basket means the
    customer already clicked checkout and is somewhere in/after the Razorpay
    gateway - that's the checkout-dropoff thread's territory (app/dropoff.py,
    keyed off Razorpay's own "attempted" status), not pre-checkout silent
    abandonment, which by definition means the pay button was never clicked.
    Guards the cart-idle sweep (run_cart_idle_sweep) - and the debug button,
    since both share _handle_cart_event - from conflating the two: /checkout
    never clears cart_json, so a customer idling at the gateway would
    otherwise look exactly like one who never tried to pay at all."""
    candidates = db.query(Order).filter(
        Order.customer_id == customer_id,
        Order.status == OrderStatus.CREATED,
    ).all()
    for candidate in candidates:
        try:
            if _items_match(json.loads(candidate.items_json), cart):
                return candidate
        except (TypeError, json.JSONDecodeError):
            continue
    return None


def _handle_cart_event(db: Session, customer: Customer, event_type: CartEventType, cart: list[dict], amount_inr: float) -> dict:
    customer = refresh_tier(db, customer)

    open_failed = _open_failed_run_for_items(db, customer.id, cart)
    if open_failed:
        # This exact basket already has a failed payment attempt awaiting
        # Retry or Give-up - its money is tracked in at_risk_failed. Don't
        # log a duplicate abandonment/cancellation, which would book a
        # second, independent at-risk entry for the same real money.
        return {
            "cart_event_id": None,
            "action": AgentAction.NO_ACTION.value,
            "confidence": None,
            "reasoning": (
                f"This cart already has a failed payment attempt in progress "
                f"(₹{open_failed.amount_inr:.0f}) - not logging a duplicate "
                "abandonment/cancellation. Use Retry or Give up on the checkout "
                "failure banner to resolve it first."
            ),
            "tier": customer.tier.value,
            "items": cart,
            "original_amount_inr": amount_inr,
            "status": None,
            "incentive_pct": None,
            "final_amount_inr": None,
            "deduplicated": True,
            "note": "Open failed-payment run for this exact basket.",
        }

    # Windowed, not all-time. Counting cancels forever meant a customer who
    # cancelled three carts once stayed escalated to human review
    # permanently, with no path back no matter how well they behaved after.
    cancel_window_start = datetime.utcnow() - timedelta(
        days=runtime_flags.get_tier_behavior_window_days()
    )
    repeat_cancels = db.query(CartEvent).filter(
        CartEvent.customer_id == customer.id,
        CartEvent.event_type == CartEventType.EXPLICIT_CANCEL,
        CartEvent.created_at >= cancel_window_start,
    ).count()

    if event_type == CartEventType.SILENT_ABANDON:
        # Don't log a silent abandonment while an open checkout for this
        # exact basket is still in flight at the gateway - see
        # _open_created_order_for_items. Checked before the dedup lookup
        # below since it's a different reason to no-op, not a repeat.
        open_created = _open_created_order_for_items(db, customer.id, cart)
        if open_created:
            return {
                "cart_event_id": None,
                "action": AgentAction.NO_ACTION.value,
                "confidence": None,
                "reasoning": (
                    f"This cart matches an open checkout in progress "
                    f"(razorpay_order_id={open_created.razorpay_order_id}) - not "
                    "logging a silent abandonment while the customer may still be "
                    "completing payment at the gateway. This basket belongs to the "
                    "checkout-dropoff thread, not pre-checkout cart abandonment."
                ),
                "tier": customer.tier.value,
                "items": cart,
                "original_amount_inr": amount_inr,
                "status": None,
                "incentive_pct": None,
                "final_amount_inr": None,
                "deduplicated": True,
                "note": "Open in-progress checkout (Order.status=='created') for this exact basket.",
            }

    # Idempotency for silent abandons: same cart, still unresolved, already
    # logged -> return the existing decision instead of logging a second
    # loss for the same money. See _find_open_duplicate_abandon.
    if event_type == CartEventType.SILENT_ABANDON:
        duplicate = _find_open_duplicate_abandon(db, customer, cart)
        if duplicate:
            # Same shape as a fresh decision - the store banner renders it
            # identically (it used to lack items/amount/tier, which crashed
            # the banner and made repeat timeouts look like nothing
            # happened). Discount terms are echoed only while the offer is
            # actually still PENDING - a declined-but-still-tracked offer
            # must re-present as a plain reminder, not re-dangle a discount
            # the customer already said no to.
            still_pending = duplicate.status == CartEventStatus.PENDING
            return {
                "cart_event_id": duplicate.id,
                "action": duplicate.action.value if duplicate.action else None,
                "confidence": duplicate.confidence,
                "reasoning": duplicate.reasoning,
                "tier": customer.tier.value,
                "items": json.loads(duplicate.items_json or "[]"),
                "original_amount_inr": duplicate.amount_inr,
                "status": duplicate.status.value if duplicate.status else None,
                "incentive_pct": duplicate.incentive_pct if still_pending else None,
                "final_amount_inr": duplicate.incentive_final_amount_inr if still_pending else None,
                "deduplicated": True,
                "note": (
                    "This cart was already logged as abandoned and hasn't been "
                    "bought since - returning the original decision rather than "
                    "recording the same loss twice."
                ),
            }

    # Computed once here and reused for both the tier gate and the discount
    # size, so the tier a customer is judged by and the % they're offered
    # can never come from two different reads of their history.
    engagement = tier_breakdown(db, customer.id)

    # The three independent incentive gates - computed here (the caller),
    # same pattern as the frequency cap always was, so all three can be
    # stored on the CartEvent row and audited individually instead of
    # collapsed into one ambiguous flag.
    freq_cap_ok = _cart_freq_cap_ok(db, customer.id, customer.tier)
    amount_cap_ok = amount_inr <= runtime_flags.get_incentive_max_order_value(customer.tier.value)
    tier_incentive_eligible = customer.tier.value in runtime_flags.get_incentive_eligible_tiers()

    action, confidence, reasoning = rule_based_cart_event_decision(
        event_type=event_type,
        tier=customer.tier,
        amount_inr=amount_inr,
        repeat_cancel_count=repeat_cancels + (1 if event_type == CartEventType.EXPLICIT_CANCEL else 0),
        amount_cap_ok=amount_cap_ok,
        freq_cap_ok=freq_cap_ok,
    )

    incentive_pct = None
    incentive_final_amount_inr = None
    if action == AgentAction.OFFER_INCENTIVE:
        # No longer one flat rate for everyone. The customer's position
        # inside their own tier's engagement-score band decides where they
        # land inside that tier's discount band - so a customer at the top
        # of Regular gets close to 20%, one who just scraped in gets close
        # to 10%. Deterministic: no LLM on this hot path, no fallback to
        # design, and the audit trail can always reproduce the number.
        incentive_pct = incentive_pct_for_customer(customer.tier, engagement["score"])
        raw_final = amount_inr * (1 - incentive_pct / 100)
        incentive_final_amount_inr = round_to_paise_safe(raw_final)

    # A status + expiry means "this is a redeemable offer with terms /checkout
    # must honor or void" - needed for anything with a discount attached
    # (silent_abandon OR explicit_cancel), and also for explicit_cancel's
    # plain resume-link (no money term, but still something to act on).
    # A silent_abandon plain reminder has neither - nothing for the customer
    # to redeem, so it stays untracked here (see the invisible
    # PendingSignal marker below instead).
    status = None
    expires_at = None
    if action == AgentAction.OFFER_INCENTIVE or (event_type == CartEventType.EXPLICIT_CANCEL and action == AgentAction.SEND_RESUME_LINK):
        status = CartEventStatus.PENDING
        expires_at = datetime.utcnow() + timedelta(hours=runtime_flags.get_nudge_expiry_hours())

    cart_event = CartEvent(
        customer_id=customer.id,
        event_type=event_type,
        items_json=json.dumps(cart),
        amount_inr=amount_inr,
        tier_at_time=customer.tier,
        action=action,
        confidence=confidence,
        reasoning=reasoning,
        status=status,
        expires_at=expires_at,
        incentive_pct=incentive_pct,
        incentive_final_amount_inr=incentive_final_amount_inr,
        amount_cap_ok=amount_cap_ok,
        freq_cap_ok=freq_cap_ok,
        tier_incentive_eligible=tier_incentive_eligible,
    )
    db.add(cart_event)
    db.commit()
    db.refresh(cart_event)

    write_audit_entry(
        db,
        action_type="cart_event_detected",
        details={
            "event_type": event_type.value, "tier": customer.tier.value,
            "amount_inr": amount_inr, "action": action.value, "confidence": confidence,
            "amount_cap_ok": amount_cap_ok, "freq_cap_ok": freq_cap_ok,
            "tier_incentive_eligible": tier_incentive_eligible,
            "incentive_final_amount_inr": incentive_final_amount_inr,
        },
        event_id=cart_event.id,
    )

    success, outcome = execute_action(action.value, cart_event)
    cart_event.outcome = outcome
    db.commit()

    write_audit_entry(
        db,
        action_type="cart_event_action_executed",
        details={"action": action.value, "success": success, "outcome": outcome},
        event_id=cart_event.id,
    )

    # An explicit cancel is the strongest pre-checkout CART signal, so it
    # consolidates every softer piece of open cart-thread recovery state
    # this customer has - pending offer cards (cancelling the cart IS
    # answering "no" to the offer riding it) and live reminder-attribution
    # markers. Each one's at-risk is released here and the cancel re-books
    # the money below. Failed-PAYMENT money is deliberately NOT touched:
    # it lives in its own at_risk_failed bucket with its own exits
    # (retry-capture -> recovered, /checkout/give-up-failed or sweep lapse
    # -> lost) - see webhook._sync_storefront_order_failed.
    superseded_amounts = []
    if event_type == CartEventType.EXPLICIT_CANCEL:
        open_offers = db.query(CartEvent).filter(
            CartEvent.customer_id == customer.id,
            CartEvent.status == CartEventStatus.PENDING,
            CartEvent.id != cart_event.id,   # never the card THIS event just created
        ).all()
        for offer in open_offers:
            # DECLINED, not silently superseded - the tier's responsiveness
            # score should see that a nudge was answered with a cancel.
            offer.status = CartEventStatus.DECLINED
            offer.resolved_at = datetime.utcnow()
            db.commit()
            prior_bucket = ("at_risk_declined" if offer.event_type == CartEventType.EXPLICIT_CANCEL
                            else "at_risk_soft")
            adjust_revenue(db, prior_bucket, -offer.amount_inr,
                           reason="cart_event:offer_superseded_by_cancel")
            superseded_amounts.append(offer.amount_inr)

        live_signals = db.query(PendingSignal).filter(
            PendingSignal.customer_id == customer.id,
            PendingSignal.kind == PendingSignalKind.TIMEOUT_ATTRIBUTION,
            PendingSignal.consumed_at.is_(None),
            PendingSignal.expires_at > datetime.utcnow(),
        ).all()
        for sig in live_signals:
            sig.consumed_at = datetime.utcnow()
            db.commit()
            prior = db.query(CartEvent).filter(CartEvent.id == sig.cart_event_id).first()
            if prior:
                prior_bucket = ("at_risk_declined" if prior.event_type == CartEventType.EXPLICIT_CANCEL
                                else "at_risk_soft")
                adjust_revenue(db, prior_bucket, -prior.amount_inr,
                               reason="cart_event:reminder_superseded_by_cancel")
                superseded_amounts.append(prior.amount_inr)

    # Bucket the at-risk amount at the FULL original value - a discount is a
    # cost paid later to recover it, not a reduction in what was at risk.
    # See resolve_cart_recovery() in revenue.py for how the discount cost
    # gets booked, at capture time, if this ends up being redeemed.
    bucket = "at_risk_declined" if event_type == CartEventType.EXPLICIT_CANCEL else "at_risk_soft"
    adjust_revenue(db, bucket, amount_inr, reason=f"cart_event:{event_type.value}")

    # If the cart SHRANK between the superseded tracking and this cancel,
    # the removed items' value also never converted - book that shortfall
    # straight to lost, so nothing released above can silently vanish.
    # SUM, not max: with one superseded thread (the normal case) this is
    # exactly the agreed max(offer-time, latest-cart) rule, but when
    # several distinct cart threads get consolidated at once, every
    # released rupee must either be re-booked by this cancel or land in
    # lost - keeping only the largest dropped the rest from the ledger.
    prior_total = round(sum(superseded_amounts), 2)
    shortfall = round(prior_total - amount_inr, 2)
    if superseded_amounts and shortfall > 0:
        adjust_revenue(db, "total_lost", shortfall, reason="cart_lost:cart_shrunk_before_cancel")
        write_audit_entry(
            db, action_type="cart_recovery_lost",
            details={"cart_event_id": cart_event.id, "amount_inr": shortfall,
                      "event_type": event_type.value, "reason": "cart_shrunk_before_cancel"},
            event_id=cart_event.id,
        )

    # A cancel the agent deliberately doesn't chase (no_action - risk/new
    # tiers) has no recovery path from the moment it's logged: book the
    # loss right away instead of leaving it "at risk" with no exit.
    if event_type == CartEventType.EXPLICIT_CANCEL and action == AgentAction.NO_ACTION:
        resolve_cart_loss(db, cart_event, "no_recovery_attempted")

    # Invisible attribution marker for silent-abandon reminders (no money
    # term attached): lets a *later*, ordinary checkout still be honestly
    # credited to the nudge. Incentive offers don't need this - they're
    # already tracked via CartEvent.status above, which /checkout checks first.
    if event_type == CartEventType.SILENT_ABANDON and action != AgentAction.OFFER_INCENTIVE:
        db.add(PendingSignal(
            customer_id=customer.id,
            kind=PendingSignalKind.TIMEOUT_ATTRIBUTION,
            cart_event_id=cart_event.id,
            action=action.value,
            reasoning=reasoning,
            expires_at=datetime.utcnow() + timedelta(hours=runtime_flags.get_nudge_expiry_hours()),
        ))
        db.commit()

    return {
        "cart_event_id": cart_event.id,
        "action": action.value, "confidence": confidence, "reasoning": reasoning,
        "outcome": outcome, "tier": customer.tier.value,
        "status": status.value if status else None,
        "items": cart,
        "original_amount_inr": amount_inr,
        "incentive_pct": incentive_pct,
        "final_amount_inr": incentive_final_amount_inr,
    }


class SimulateCartTimeoutRequest(BaseModel):
    pass


@router.post("/debug/simulate-cart-timeout")
def simulate_cart_timeout(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    """Demo button standing in for 'N minutes of inactivity with items still in
    cart, no explicit action taken' - fires the same pipeline run_cart_idle_sweep
    (below) fires automatically, right now instead of waiting out the real
    cart_idle_after_minutes threshold. Kept for tutorial/demo use, since waiting
    out a real timeout isn't practical on camera."""
    cart = _cart(customer)
    if not cart:
        raise HTTPException(status_code=400, detail="Cart is empty - add an item first to simulate this")
    amount = _cart_amount(cart)
    result = _handle_cart_event(db, customer, CartEventType.SILENT_ABANDON, cart, amount)
    return {"status": "silent_abandon_simulated", "recovery_decision": result}


# ── Cart-idle sweep (the real, automatic version of the debug button above) ──

def run_cart_idle_sweep():
    """Background loop: a customer's cart, non-empty and untouched for
    cart_idle_after_minutes, gets treated as a genuine silent abandonment -
    same _handle_cart_event(..., SILENT_ABANDON, ...) pipeline the debug
    button above fires manually. Automatic, and independent of whether the
    customer is logged in, logged out, or comes back days later: cart_json
    lives on the Customer row, looked up fresh by id, with no session state
    involved at all (see app/auth.py - tokens are stateless, there's no
    server-side session to expire).

    Open-ended "older than cutoff" query, not a bounded trailing window like
    app/dropoff.py's poller - that shape has a real bug (anything already
    older than the window when a poll fires falls out of it forever). Here,
    a cart idle for 2 hours is just as much a hit as one idle for 31 minutes;
    nothing can permanently slip through a timing gap.

    Safe to run every interval over the same still-idle cart:
    _find_open_duplicate_abandon (inside _handle_cart_event) returns the
    original decision instead of re-booking at_risk_soft, and
    _open_created_order_for_items (also inside _handle_cart_event) skips any
    basket that matches an in-progress checkout - that belongs to the
    checkout-dropoff thread (app/dropoff.py), not this one.
    """
    interval = max(30, settings.cart_idle_sweep_interval_minutes * 60)
    while True:
        try:
            db = SessionLocal()
            try:
                cutoff = datetime.utcnow() - timedelta(
                    minutes=runtime_flags.get_cart_idle_after_minutes()
                )
                candidates = db.query(Customer).filter(
                    Customer.cart_json != "[]",
                    Customer.cart_updated_at < cutoff,
                ).all()
                for customer in candidates:
                    cart = _cart(customer)
                    if not cart:
                        continue
                    amount = _cart_amount(cart)
                    _handle_cart_event(db, customer, CartEventType.SILENT_ABANDON, cart, amount)
            finally:
                db.close()
        except Exception:
            logger.exception("cart idle sweep failed; will retry next interval")
        time.sleep(interval)


# ── Nudge surfacing: what should the cart page show right now? ──────────────

def _expire_stale_cancel_offer(db: Session, cart_event: CartEvent) -> CartEvent:
    if (
        cart_event.status == CartEventStatus.PENDING
        and cart_event.expires_at
        and cart_event.expires_at <= datetime.utcnow()
    ):
        cart_event.status = CartEventStatus.EXPIRED
        cart_event.resolved_at = datetime.utcnow()
        db.commit()
        resolve_cart_loss(db, cart_event, "offer_expired")
    return cart_event


@router.get("/cart/pending-signals")
def pending_signals(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    """Called once when the cart page loads. Returns at most two things:
    - cancel_offer: a non-blocking resume/incentive card for an unresolved
      explicit-cancel decision, if any (lazily expired here if stale)
    - payment_failure_notice: a one-shot toast about a payment that failed
      while the customer wasn't on the checkout screen; consumed on read.
    Both are independent of, and never gate, the normal pay button."""
    cancel_offer = None
    pending_cart_event = (
        db.query(CartEvent)
        .filter(
            CartEvent.customer_id == customer.id,
            CartEvent.event_type == CartEventType.EXPLICIT_CANCEL,
            CartEvent.status == CartEventStatus.PENDING,
        )
        .order_by(CartEvent.created_at.desc())
        .first()
    )
    if pending_cart_event:
        pending_cart_event = _expire_stale_cancel_offer(db, pending_cart_event)
        if pending_cart_event.status == CartEventStatus.PENDING:
            cancel_offer = {
                "cart_event_id": pending_cart_event.id,
                "items": json.loads(pending_cart_event.items_json),
                "original_amount_inr": pending_cart_event.amount_inr,
                "incentive_pct": pending_cart_event.incentive_pct,
                "final_amount_inr": pending_cart_event.incentive_final_amount_inr,
                "action": pending_cart_event.action.value if pending_cart_event.action else None,
            }

    payment_failure_notice = None
    notice_signal = (
        db.query(PendingSignal)
        .filter(
            PendingSignal.customer_id == customer.id,
            PendingSignal.kind == PendingSignalKind.PAYMENT_FAILURE_NOTICE,
            PendingSignal.consumed_at.is_(None),
        )
        .order_by(PendingSignal.created_at.desc())
        .first()
    )
    if notice_signal:
        if notice_signal.expires_at > datetime.utcnow():
            payment_failure_notice = {
                "action": notice_signal.action,
                "failure_reason": notice_signal.failure_reason,
            }
        # Consumed either way - a stale notice shouldn't linger and surface
        # later once it's no longer timely.
        notice_signal.consumed_at = datetime.utcnow()
        db.commit()

    return {"cancel_offer": cancel_offer, "payment_failure_notice": payment_failure_notice}


class CartEventIdRequest(BaseModel):
    cart_event_id: str


@router.post("/cart/resume")
def resume_cancelled_cart(
    req: CartEventIdRequest,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    """Customer clicked 'resume' on the cancel-offer card. Restores the
    saved items into their live, editable cart - does NOT jump straight to
    Razorpay. They land back on the normal cart screen and can add/remove
    items before paying, same as any other cart. The offer itself stays
    PENDING (not yet RESUMED) - /checkout is what actually validates and
    redeems it, checking whether the cart still exactly matches what was
    offered by the time they hit pay."""
    cart_event = db.query(CartEvent).filter(
        CartEvent.id == req.cart_event_id,
        CartEvent.customer_id == customer.id,
    ).first()
    if not cart_event:
        raise HTTPException(status_code=404, detail="No matching cancelled cart found")

    cart_event = _expire_stale_cancel_offer(db, cart_event)
    if cart_event.status != CartEventStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"This offer is no longer active ({cart_event.status.value})")

    saved_items = json.loads(cart_event.items_json)
    cart = _cart(customer)
    for saved in saved_items:
        for item in cart:
            if item["sku"] == saved["sku"]:
                item["qty"] += saved["qty"]
                break
        else:
            cart.append(dict(saved))
    customer.cart_json = json.dumps(cart)
    customer.cart_updated_at = datetime.utcnow()
    db.commit()

    write_audit_entry(
        db, action_type="cart_event_resume_clicked",
        details={"cart_event_id": cart_event.id, "restored_items": saved_items},
    )

    return _cart_response(db, customer)


@router.post("/cart/decline-resume")
def decline_resume(
    req: CartEventIdRequest,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    """Customer explicitly dismissed the cancel-resume card ('no thanks').
    The only remaining caller is the explicit-cancel resume card - the
    silent-abandon offer has no decline action any more (a customer who
    keeps shopping never needs to opt out of a discount that's already
    following their cart). An explicit-cancel resume card is always
    declined with an empty cart (cancelling clears it), so this always
    closes the recovery path for good."""
    cart_event = db.query(CartEvent).filter(
        CartEvent.id == req.cart_event_id,
        CartEvent.customer_id == customer.id,
    ).first()
    if not cart_event:
        raise HTTPException(status_code=404, detail="No matching cancelled cart found")

    if cart_event.status == CartEventStatus.PENDING:
        cart_event.status = CartEventStatus.DECLINED
        cart_event.resolved_at = datetime.utcnow()
        db.commit()
        resolve_cart_loss(db, cart_event, "offer_declined")
        write_audit_entry(db, action_type="cart_event_declined", details={"cart_event_id": cart_event.id})

    return {"status": cart_event.status.value}

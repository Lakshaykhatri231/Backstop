import hashlib
import hmac
import json

from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from datetime import datetime, timedelta

from app.config import settings
from app.database import get_db, SessionLocal
from app.models import (
    Event, Decision, EventType, FailureReason, AgentAction, Order, OrderStatus,
    WebhookEventLog, PendingSignal, PendingSignalKind,
)
from app.rules_engine import rule_based_decision  # noqa: F401 (used indirectly via llm_agent)
from app.llm_agent import decide
from app.actions import execute_action
from app.audit import write_audit_entry
from app.revenue import adjust as adjust_revenue, record_capture_revenue, resolve_cart_to_failed_thread
from app.tiering import refresh_tier
from app.storefront import _items_match
from app import runtime_flags

router = APIRouter()


def verify_signature(raw_body: bytes, signature: str) -> bool:
    if not settings.razorpay_webhook_secret:
        # No secret configured (e.g. local dev without real Razorpay account yet).
        # We deliberately do NOT silently accept - this should be treated as
        # a configuration error, not a bypass, once you're wired to real Razorpay.
        return False
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _map_event_type(raw: str) -> EventType | None:
    try:
        return EventType(raw)
    except ValueError:
        return None


def _extract_error_fields(entity: dict) -> dict:
    """Pulls Razorpay's raw error fields verbatim - never invented, never
    normalized here. See _classify_failure_reason for the separate
    normalization step."""
    return {
        "razorpay_error_code": entity.get("error_code"),
        "razorpay_error_description": entity.get("error_description"),
        "razorpay_error_reason": entity.get("error_reason"),
        "razorpay_error_source": entity.get("error_source"),
        "razorpay_error_step": entity.get("error_step"),
    }


def _classify_failure_reason(entity: dict) -> FailureReason:
    """
    Maps Razorpay's raw error fields to our application-level FailureReason.

    Deliberately does NOT treat generic codes like BAD_REQUEST_ERROR or
    GATEWAY_ERROR as an automatic BANK_DECLINE - those codes are too generic
    to safely infer a specific reason, and doing so would misclassify things
    like cancellations or invalid-card errors as bank declines. Anything that
    can't be confidently classified from the actual reason/description text
    falls through to UNKNOWN, which is a real, honest category the rules
    engine already knows to escalate on.
    """
    reason = (entity.get("error_reason") or "").lower()
    description = (entity.get("error_description") or "").lower()
    combined = f"{reason} {description}"

    if "insufficient" in combined:
        return FailureReason.INSUFFICIENT_FUNDS
    if "expired" in combined:
        return FailureReason.CARD_EXPIRED
    if "authentication" in combined or "otp" in combined:
        return FailureReason.AUTHENTICATION_FAILED
    if "cancel" in combined:
        return FailureReason.CANCELLED
    if "invalid" in combined and "card" in combined:
        return FailureReason.INVALID_CARD
    if "international" in combined:
        return FailureReason.INVALID_CARD
    if "risk" in combined or "fraud" in combined:
        return FailureReason.RISK_BLOCK
    if "declined" in combined and ("bank" in combined or "issuer" in combined):
        return FailureReason.BANK_DECLINE
    if "network" in combined or "timeout" in combined or "timed_out" in combined or "gateway" in combined:
        return FailureReason.NETWORK_ERROR
    return FailureReason.UNKNOWN


def _handle_payment_captured(db: Session, inner_payload: dict) -> dict:
    payment_entity = inner_payload.get("payment", {}).get("entity", {})
    razorpay_order_id = payment_entity.get("order_id")
    amount_inr = (payment_entity.get("amount", 0) or 0) / 100.0

    order = (
        db.query(Order).filter(Order.razorpay_order_id == razorpay_order_id).first()
        if razorpay_order_id else None
    )

    if order is None:
        # Real money came in but we have no matching storefront order (e.g. a
        # payment created outside /checkout). Still record the revenue - it's
        # real - but there's no local order/tier state to sync.
        write_audit_entry(
            db, action_type="payment_captured",
            details={"razorpay_order_id": razorpay_order_id, "amount_inr": amount_inr,
                      "matched_storefront_order": False},
        )
        adjust_revenue(db, "total_revenue", amount_inr, reason="payment_captured_unmatched", order_id=razorpay_order_id)
        return {"status": "processed", "matched_storefront_order": False, "amount_inr": amount_inr}

    if order.status == OrderStatus.CAPTURED:
        # /checkout/verify's fast-path confirmation raced ahead of this
        # webhook - the normal case - and nowadays it books revenue itself.
        # Still call record_capture_revenue: its exactly-once flag decides
        # whether anything is left to do, which covers orders captured
        # before that fix existed and any capture verify confirmed but
        # failed to book. Prior failure is inferred from failure_reason
        # (set by _sync_storefront_order_failed and never cleared on
        # recapture), since the FAILED->CAPTURED transition itself is no
        # longer observable from this branch.
        booked_now = record_capture_revenue(
            db, order, was_recovery=order.failure_reason is not None
        )
        write_audit_entry(
            db, action_type="payment_captured_already_processed",
            details={"razorpay_order_id": razorpay_order_id, "amount_inr": amount_inr,
                      "revenue_booked_by_this_webhook": booked_now},
        )
        return {"status": "already_processed", "matched_storefront_order": True,
                "amount_inr": amount_inr, "revenue_booked_by_this_webhook": booked_now}

    was_recovery = order.status == OrderStatus.FAILED
    order.status = OrderStatus.CAPTURED
    order.razorpay_payment_id = payment_entity.get("id")
    order.resolved_at = datetime.utcnow()
    db.commit()
    refresh_tier(db, order.customer)

    write_audit_entry(
        db, action_type="payment_captured",
        details={"razorpay_order_id": razorpay_order_id, "amount_inr": amount_inr,
                  "matched_storefront_order": True, "was_recovery": was_recovery},
    )

    # Shared exactly-once booking (total_revenue, at-risk -> recovered on a
    # recovery, cart-event closeout + incentive_cost) - same function
    # /checkout/verify uses, so whichever path runs first does it all and
    # the other no-ops.
    record_capture_revenue(db, order, was_recovery)

    return {"status": "processed", "event": "payment.captured", "amount_inr": amount_inr,
            "matched_storefront_order": True, "was_recovery": was_recovery,
            "recovered_from_cart_event_id": order.recovered_from_cart_event_id}


def _sync_storefront_order_failed(db: Session, event: Event) -> None:
    """Best-effort: if this payment.failed event corresponds to a real
    storefront order (created via /checkout), mark it failed and add its
    amount to the at-risk ledger. No-ops silently for events that don't
    originate from the storefront (e.g. scripts/simulate_webhook.py's
    synthetic subscription payloads) - matching on razorpay_order_id only.

    Idempotent by construction: only acts when order.status == CREATED, so a
    duplicate payment.failed delivery for the same order (status already
    FAILED) is a no-op, and a CAPTURED order can never be moved back to
    FAILED by a late/duplicate failure webhook."""
    razorpay_order_id = None
    if event.raw_payload:
        try:
            payload = json.loads(event.raw_payload)
            razorpay_order_id = (
                payload.get("payload", {}).get("payment", {}).get("entity", {}).get("order_id")
            )
        except json.JSONDecodeError:
            pass
    if not razorpay_order_id:
        return

    order = db.query(Order).filter(Order.razorpay_order_id == razorpay_order_id).first()
    if not order or order.status != OrderStatus.CREATED:
        return

    order.status = OrderStatus.FAILED
    order.failure_reason = event.failure_reason
    order.resolved_at = datetime.utcnow()
    db.commit()
    refresh_tier(db, order.customer)

    # Book the at-risk amount ONCE per purchase run, not once per retry.
    #
    # /checkout mints a fresh Order per retry, so this handler fires again
    # for every try at the same purchase. The booking gate is whether the
    # run already has an OPEN carrier: a prior FAILED order for the same
    # basket whose risk_settled flag is still False. Gating on the
    # attempt COUNT (the previous fix) was subtly wrong: attempt counting
    # sees every prior failed row for the basket, including seeded history
    # and long-dead runs whose at-risk was never booked or was already
    # settled - so a genuinely new failure could be "skipped as a retry"
    # of money that no ledger bucket was tracking at all. risk_settled is
    # the same source of truth the recovery/loss side settles against, so
    # booking and settlement can never disagree about who carries the run.
    carrier = _open_run_carrier(db, order)
    if carrier is None:
        # This order IS the run's carrier. If it also carries a cart-thread
        # origin (a resumed offer, a resumed cancel-resume card, or a plain
        # reminder attribution), that origin's booking has been sitting
        # untouched in at_risk_soft/declined since it was created - release
        # it now, since the money's fate has just moved to the
        # failed-payment thread. Only the carrier-creating failure of a run
        # can ever carry this: a retry within the same run is never
        # re-attributed (see _resolve_storefront_attempt_count / /checkout),
        # so this runs at most once per origin.
        if order.recovered_from_cart_event_id:
            resolve_cart_to_failed_thread(db, order)
        # Book the risk, leave it open.
        adjust_revenue(db, "at_risk_failed", event.amount_inr, reason="payment_failed", order_id=razorpay_order_id)
    else:
        # A retry: the money is already tracked by the carrier. Mark this
        # try settled immediately so it can never be mistaken for a
        # carrier by later settlement passes.
        order.risk_settled = True
        db.commit()
        write_audit_entry(
            db,
            action_type="at_risk_booking_skipped",
            details={
                "razorpay_order_id": razorpay_order_id,
                "amount_inr": event.amount_inr,
                "carrier_razorpay_order_id": carrier.razorpay_order_id,
                "reason": "retry of a purchase whose at-risk is already carried by an earlier open failure",
            },
        )


def _open_run_carrier(db: Session, order: Order) -> Order | None:
    """The earlier FAILED order (same customer, same basket signature)
    that still carries this run's at-risk booking, if one exists. Open =
    risk_settled is False; settled seed rows, recovered runs, and lapsed
    runs never suppress a fresh booking."""
    try:
        current_items = json.loads(order.items_json)
    except (TypeError, json.JSONDecodeError):
        return None
    candidates = db.query(Order).filter(
        Order.customer_id == order.customer_id,
        Order.status == OrderStatus.FAILED,
        Order.risk_settled.is_(False),
        Order.id != order.id,
    ).all()
    for candidate in candidates:
        try:
            candidate_items = json.loads(candidate.items_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if _items_match(candidate_items, current_items):
            return candidate
    return None


def _resolve_storefront_attempt_count(db: Session, razorpay_order_id: str | None) -> int | None:
    """Razorpay's subscription payment_attempts field only exists on
    subscription entities - it's simply absent for one-off storefront
    checkout payments, because /checkout mints a brand-new Razorpay order
    (and a brand-new Order row) on every single retry, including the
    retry_now flow. That's why attempt_count was silently defaulting to 1
    on every failure: `sub_entity.get("payment_attempts", 1) or 1` falls
    back to 1 whenever sub_entity is {}, which it always is here.

    Reconstruct the real attempt count instead: find the storefront Order
    this failed payment belongs to, then count that same customer's
    consecutive FAILED orders for the SAME cart (via _items_match - the
    same order-independent sku:qty comparison already used for incentive
    validation) since their last successful capture. Scoping by items,
    not just by customer, matters: a customer's unrelated failed purchase
    from an earlier session (different product, different day) is not an
    "attempt" at THIS checkout, and must not push a brand-new purchase
    straight past retry_now into escalation. Returns None when this event
    doesn't correspond to a real storefront order (e.g. synthetic webhook
    payloads from scripts/simulate_webhook.py), so the caller can fall
    back to 1.
    """
    if not razorpay_order_id:
        return None
    order = db.query(Order).filter(Order.razorpay_order_id == razorpay_order_id).first()
    if not order:
        return None

    try:
        current_items = json.loads(order.items_json)
    except (TypeError, json.JSONDecodeError):
        current_items = None

    last_captured = (
        db.query(Order)
        .filter(Order.customer_id == order.customer_id, Order.status == OrderStatus.CAPTURED)
        .order_by(Order.resolved_at.desc())
        .first()
    )
    prior_failed_query = db.query(Order).filter(
        Order.customer_id == order.customer_id,
        Order.status == OrderStatus.FAILED,
    )
    if last_captured and last_captured.resolved_at:
        prior_failed_query = prior_failed_query.filter(Order.created_at > last_captured.resolved_at)

    if current_items is None:
        # Can't compare carts - fall back to the old (broader) count rather
        # than silently returning 1 for a possibly-real retry streak.
        return prior_failed_query.count() + 1

    prior_failures = 0
    for candidate in prior_failed_query.all():
        try:
            candidate_items = json.loads(candidate.items_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if _items_match(candidate_items, current_items):
            prior_failures += 1

    return prior_failures + 1


def _process_decision_and_action(event_id: str) -> None:
    """
    Runs the (potentially slow, LLM-backed) decision + action-execution step
    in the background, after the webhook has already returned 200 to Razorpay.

    Opens its own DB session rather than reusing the request-scoped one,
    since this runs after the original request/response cycle has completed.
    """
    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.id == event_id).first()
        if not event:
            return

        result = decide(
            failure_reason=event.failure_reason,
            attempt_count=event.attempt_count,
            amount_inr=event.amount_inr,
            subscription_id=event.subscription_id,
        )

        if result["source"] == "rules_engine_fallback":
            write_audit_entry(
                db, action_type="llm_failure_fallback",
                details={"error": result["llm_error"]}, event_id=event.id,
            )

        action = result["action"]
        confidence = result["confidence"]

        # retry_now is exempt from the confidence gate: it's the lowest-stakes
        # action in the system (equivalent to the customer clicking pay again)
        # and already has its own safety net — two unclassifiable failures in
        # a row escalate on the very next attempt regardless of confidence.
        # unknown/invalid_card are inherently uncertain by definition, so gating
        # retry_now on confidence would silently kill that path entirely.
        gate_exempt_actions = {AgentAction.ESCALATE_TO_HUMAN.value, AgentAction.RETRY_NOW.value}

        escalated = False
        if confidence < runtime_flags.get_confidence_threshold() and action not in gate_exempt_actions:
            escalated = True
            original_action = action
            action = AgentAction.ESCALATE_TO_HUMAN.value
            result["reasoning"] = (
                f"[Confidence gate override] Original decision was '{original_action}' "
                f"at confidence {confidence:.2f}, below threshold "
                f"{runtime_flags.get_confidence_threshold()}. Forced to escalate. "
                + result["reasoning"]
            )

        decision = Decision(
            event_id=event.id, action=action, confidence=confidence,
            reasoning=result["reasoning"], source=result["source"], escalated=escalated,
        )
        db.add(decision)
        db.commit()
        db.refresh(decision)

        write_audit_entry(
            db, action_type="decision_made",
            details={"action": action, "confidence": confidence, "reasoning": result["reasoning"],
                      "source": result["source"], "escalated": escalated},
            event_id=event.id, decision_id=decision.id,
        )

        success, outcome = execute_action(action, event)
        decision.executed = True
        decision.outcome = outcome
        db.commit()

        write_audit_entry(
            db, action_type="action_executed",
            details={"action": action, "success": success, "outcome": outcome},
            event_id=event.id, decision_id=decision.id,
        )

        # If this failure corresponds to a real storefront order, leave a
        # one-shot notice for the cart page to show next time the customer
        # is there - covers the case where they've already navigated away
        # by the time this async decision finishes resolving. Incentives are
        # deliberately never offered here (see rules_engine.rule_based_decision -
        # this path only ever produces retry/escalate outcomes).
        if event.event_type == EventType.PAYMENT_FAILED and event.raw_payload:
            try:
                payload = json.loads(event.raw_payload)
                razorpay_order_id = (
                    payload.get("payload", {}).get("payment", {}).get("entity", {}).get("order_id")
                )
            except json.JSONDecodeError:
                razorpay_order_id = None

            order = (
                db.query(Order).filter(Order.razorpay_order_id == razorpay_order_id).first()
                if razorpay_order_id else None
            )
            if order:
                db.add(PendingSignal(
                    customer_id=order.customer_id,
                    kind=PendingSignalKind.PAYMENT_FAILURE_NOTICE,
                    order_id=order.id,
                    action=action,
                    reasoning=result["reasoning"],
                    failure_reason=event.failure_reason.value if event.failure_reason else None,
                    expires_at=datetime.utcnow() + timedelta(hours=settings.nudge_expiry_hours),
                ))
                db.commit()
    finally:
        db.close()


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_signature(raw_body, signature):
        # Log the rejection too - a rejected/forged webhook attempt is itself
        # an auditable security event, not something to fail silently on.
        write_audit_entry(
            db,
            action_type="webhook_rejected_bad_signature",
            details={"headers_present": bool(signature)},
        )
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed JSON payload")

    raw_event_name = payload.get("event", "")

    # Razorpay's unique event ID lives in the X-Razorpay-Event-Id header - NOT
    # in payload["event"], which is only the event TYPE (e.g. "payment.failed")
    # and is identical across every delivery of that event type. Using it as
    # a dedup key would have treated every failed payment as a duplicate of
    # every other one. Local dev tools that predate this header (or manual
    # curl tests) fall back to a hash of the raw body, which still dedupes
    # correctly for genuine redeliveries of the same payload.
    razorpay_event_id = request.headers.get("X-Razorpay-Event-Id")
    if not razorpay_event_id:
        razorpay_event_id = "bodyhash:" + hashlib.sha256(raw_body).hexdigest()

    existing = db.query(WebhookEventLog).filter(
        WebhookEventLog.razorpay_event_id == razorpay_event_id
    ).first()
    if existing:
        write_audit_entry(
            db, action_type="duplicate_webhook_ignored",
            details={"razorpay_event_id": razorpay_event_id, "event_type": raw_event_name},
        )
        return {"status": "duplicate_ignored", "razorpay_event_id": razorpay_event_id}

    db.add(WebhookEventLog(razorpay_event_id=razorpay_event_id, event_type=raw_event_name))
    db.commit()

    # payment.captured isn't part of the recovery decision pipeline at all -
    # it's pure good news. Handle it separately: sync the matching storefront
    # Order (if any) and update the merchant revenue ledger, then stop.
    if raw_event_name == "payment.captured":
        return _handle_payment_captured(db, payload.get("payload", {}))

    if raw_event_name == "payment.authorized":
        # Late-authorization case: Razorpay documents that a payment can be
        # authorized now and captured later (or auto-captured, depending on
        # your account's capture settings). We don't have a distinct pipeline
        # need for this yet - just audit it so it's visible for analytics -
        # payment.captured remains the actual revenue/recovery trigger.
        write_audit_entry(db, action_type="payment_authorized", details=payload.get("payload", {}))
        return {"status": "acknowledged", "event": "payment.authorized"}

    event_type = _map_event_type(raw_event_name)
    if event_type is None:
        # Unrecognized event type - not an error, just nothing for us to act on.
        return {"status": "ignored", "reason": "unhandled_event_type"}

    inner_payload = payload.get("payload", {})
    payment_entity = inner_payload.get("payment", {}).get("entity", {})
    sub_entity = inner_payload.get("subscription", {}).get("entity", {})

    failure_reason = _classify_failure_reason(payment_entity or sub_entity)
    error_fields = _extract_error_fields(payment_entity or sub_entity)
    amount_paise = payment_entity.get("amount", 0) or 0
    amount_inr = amount_paise / 100.0

    # Trust Razorpay's own counter when this really is a subscription retry;
    # otherwise reconstruct it from our own order history (see docstring).
    attempt_count = sub_entity.get("payment_attempts") or _resolve_storefront_attempt_count(
        db, payment_entity.get("order_id")
    ) or 1

    event = Event(
        razorpay_event_id=razorpay_event_id,
        event_type=event_type,
        subscription_id=sub_entity.get("id") or payment_entity.get("subscription_id"),
        payment_id=payment_entity.get("id"),
        customer_id=payment_entity.get("customer_id") or sub_entity.get("customer_id"),
        razorpay_order_id=payment_entity.get("order_id"),
        amount_inr=amount_inr,
        failure_reason=failure_reason,
        attempt_count=attempt_count,
        payment_method=payment_entity.get("method"),
        raw_payload=raw_body.decode("utf-8", errors="replace"),
        **error_fields,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    write_audit_entry(
        db,
        action_type="event_received",
        details={"event_type": event_type.value, "failure_reason": failure_reason.value,
                  "attempt_count": attempt_count, "amount_inr": amount_inr,
                  "razorpay_error_code": error_fields["razorpay_error_code"]},
        event_id=event.id,
    )

    if event_type == EventType.PAYMENT_FAILED:
        _sync_storefront_order_failed(db, event)

    # --- Decision + LLM enrichment + action execution happen in the
    # background, so Razorpay gets its 200 back immediately rather than
    # waiting on an LLM call. The LLM call itself already has an 8s timeout
    # with a rules-engine fallback (see app/llm_agent.py), so this endpoint
    # was already time-bounded even before backgrounding - this just removes
    # that bound from the request/response path entirely. ---
    background_tasks.add_task(_process_decision_and_action, event.id)

    return {
        "status": "accepted",
        "event_id": event.id,
        "failure_reason": failure_reason.value,
        "note": "Recovery decision is being processed asynchronously - check /outcomes/events or /audit/log shortly.",
    }

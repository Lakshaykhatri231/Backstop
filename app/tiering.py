"""
Customer tiering: turns behaviour into a tier label that the rules engine
uses to decide how much recovery effort a customer is worth.

WHY THIS IS NOT BASED ON PAYMENT SUCCESS RATE ANY MORE
------------------------------------------------------
The previous version tiered customers on captured/(captured+failed)
orders. Two things were wrong with that:

1. Most payment failures are not the customer's doing. A network error, a
   gateway timeout, or an issuer-side decline is infrastructure. Tiering
   someone down for it grades them on something they don't control.
2. It actively punished the RIGHT behaviour. /checkout mints a brand-new
   Order per retry, so a customer whose payment succeeded on the third
   try recorded 2 failures + 1 success = 33% "success rate". The more
   patiently someone retried, the worse their tier got.

So tier is now an engagement score over behaviour the customer genuinely
controls - do they finish carts they start, how often do they buy, how
much, how recently, and do they respond when nudged. Payment failures
still count, but ONLY the customer-attributable ones (expired card,
insufficient funds, ...), and only once per purchase attempt rather than
once per retry.

Recomputed after every order resolves and every cart event, plus a
periodic sweep (app/maintenance.py) so time-based components like recency
can't go stale on a dormant account.
"""
import json
import math
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    Customer, Order, OrderStatus, CartEvent, CartEventType, CartEventStatus,
    CustomerTier, FailureReason,
)
from app.audit import write_audit_entry
from app import runtime_flags


# Failures the customer is genuinely answerable for - a card they let
# expire, an account without the funds, an OTP they didn't complete.
ATTRIBUTABLE_FAILURES = {
    FailureReason.INSUFFICIENT_FUNDS,
    FailureReason.CARD_EXPIRED,
    FailureReason.INVALID_CARD,
    FailureReason.AUTHENTICATION_FAILED,
    FailureReason.RISK_BLOCK,
    FailureReason.CANCELLED,      # backed out ON the payment screen - a choice
}

# Deliberately NOT attributable: NETWORK_ERROR, BANK_DECLINE, UNKNOWN.
# These are the gateway's, the issuer's, or nobody's - grading a customer
# on them is the exact flaw this rewrite exists to remove.

# Hardcoded on purpose - see the module docstring above and CLAUDE.md: these
# are an internal scoring detail, not a merchant-tunable knob. Named here
# (rather than left as literals in engagement_score()) so nothing outside
# this module - e.g. the /insights/tier-config explainer endpoint - has to
# duplicate them and risk drifting out of sync with the actual formula.
ENGAGEMENT_WEIGHTS = {
    "completion": 30,
    "frequency": 25,
    "monetary": 20,
    "recency": 15,
    "responsiveness": 10,
}


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(x, hi))


def _item_signature(items_json: str | None) -> str:
    """Order-independent sku:qty signature, same normalization as
    storefront._items_match - two orders for the same basket collapse to
    the same key regardless of how the items were listed."""
    try:
        items = json.loads(items_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return ""
    return "|".join(sorted(f"{i.get('sku')}:{i.get('qty', 1)}" for i in items))


def purchase_attempts(db: Session, customer_id: str) -> list[dict]:
    """Collapse retries into purchase ATTEMPTS - the single most important
    correction in this module.

    /checkout creates a fresh Order (and a fresh razorpay_order_id) on
    every retry, so the Order table holds one row per *try*, not one per
    *purchase*. Counting rows means someone who retried twice before
    succeeding looks like a 33%-success customer instead of a customer who
    bought the thing.

    Grouping rule, same semantics as _resolve_storefront_attempt_count()
    in webhook.py: within one item signature, walk chronologically; a
    CAPTURED order closes the current run. So FAILED, FAILED, CAPTURED is
    ONE attempt that succeeded - not two failures and a success. A later
    purchase of the same basket starts a fresh run, so genuine repeat
    buying is still counted separately.

    Returns one dict per attempt: whether it ultimately succeeded, the
    amount, when it resolved, how many tries it took, and whether any of
    those tries failed for a customer-attributable reason.
    """
    orders = db.query(Order).filter(
        Order.customer_id == customer_id,
        Order.status.in_([OrderStatus.CAPTURED, OrderStatus.FAILED]),
    ).order_by(Order.created_at.asc()).all()

    by_signature: dict[str, list[Order]] = {}
    for o in orders:
        by_signature.setdefault(_item_signature(o.items_json), []).append(o)

    def build(run: list[Order]) -> dict:
        last = run[-1]
        succeeded = last.status == OrderStatus.CAPTURED
        reasons = {o.failure_reason for o in run if o.failure_reason}
        return {
            "succeeded": succeeded,
            "amount_inr": last.amount_inr,
            "tries": len(run),
            "started_at": run[0].created_at,
            "resolved_at": last.resolved_at or last.created_at,
            "failure_reasons": reasons,
            "attributable": bool(reasons & ATTRIBUTABLE_FAILURES),
        }

    attempts: list[dict] = []
    for run_group in by_signature.values():
        current: list[Order] = []
        for o in run_group:
            current.append(o)
            if o.status == OrderStatus.CAPTURED:
                attempts.append(build(current))
                current = []
        if current:
            attempts.append(build(current))

    attempts.sort(key=lambda a: a["started_at"])
    return attempts


def _behaviour_counts(db: Session, customer_id: str, window_days: int) -> dict:
    """Cart-side behaviour inside the rolling window.

    Windowed on purpose. The old code counted cancels for all time, so a
    customer who cancelled three carts once, then behaved perfectly for a
    year, stayed permanently penalised with no way back. Behaviour that
    old should stop counting.
    """
    since = datetime.utcnow() - timedelta(days=window_days)
    events = db.query(CartEvent).filter(
        CartEvent.customer_id == customer_id,
        CartEvent.created_at >= since,
    ).all()

    cancels = sum(1 for e in events if e.event_type == CartEventType.EXPLICIT_CANCEL)
    abandons = sum(1 for e in events if e.event_type == CartEventType.SILENT_ABANDON)

    # Nudge responsiveness: only events that actually PUT something in
    # front of the customer can be scored. A PENDING one hasn't been
    # answered yet, so it's not evidence either way.
    resumed = sum(1 for e in events if e.status == CartEventStatus.RESUMED)
    ignored = sum(
        1 for e in events
        if e.status in (CartEventStatus.DECLINED, CartEventStatus.EXPIRED)
    )

    return {
        "cancels": cancels,
        "abandons": abandons,
        "nudges_resumed": resumed,
        "nudges_ignored": ignored,
    }


def engagement_score(db: Session, customer_id: str, overrides: dict | None = None) -> dict:
    """The 0-100 score every ladder tier is derived from, plus its
    components so the merchant dashboard and the customer's own "why am I
    this tier" view can show the breakdown rather than a bare number.

    Weights are intentionally hardcoded, not merchant-tunable: the bands
    and thresholds are business decisions worth exposing, but "how much is
    recency worth relative to frequency" is an internal scoring detail
    with no natural knob-feel, and exposing it would just add four more
    ways to make the score incoherent.
    """
    overrides = overrides or {}

    def get(key, live_getter):
        return overrides[key] if key in overrides else live_getter()

    target_opm = get("tier_target_orders_per_month", runtime_flags.get_tier_target_orders_per_month)
    target_aov = get("tier_target_aov_inr", runtime_flags.get_tier_target_aov_inr)
    recency_window = get("tier_recency_window_days", runtime_flags.get_tier_recency_window_days)
    behaviour_window = get("tier_behavior_window_days", runtime_flags.get_tier_behavior_window_days)

    attempts = purchase_attempts(db, customer_id)
    succeeded = [a for a in attempts if a["succeeded"]]
    attributable_failed = [a for a in attempts if not a["succeeded"] and a["attributable"]]

    counts = _behaviour_counts(db, customer_id, behaviour_window)
    customer = db.query(Customer).filter(Customer.id == customer_id).first()

    # 1. Completion - of everything this customer started, how much did
    #    they finish? Infrastructure-only failures are excluded from the
    #    denominator entirely: they aren't evidence about the customer.
    intents = len(succeeded) + len(attributable_failed) + counts["cancels"] + counts["abandons"]
    completion_score = (len(succeeded) / intents) if intents else 0.0

    # 2. Frequency - purchases per month of tenure, against what the
    #    merchant considers a regular buyer.
    signup = customer.created_at if customer and customer.created_at else datetime.utcnow()
    tenure_months = max(1.0, (datetime.utcnow() - signup).days / 30.0)
    frequency_score = clamp(len(succeeded) / (tenure_months * target_opm)) if target_opm > 0 else 0.0

    # 3. Monetary - average captured basket against the merchant's target.
    avg_value = (sum(a["amount_inr"] for a in succeeded) / len(succeeded)) if succeeded else 0.0
    monetary_score = clamp(avg_value / target_aov) if target_aov > 0 else 0.0

    # 4. Recency - decays to 0 across the window since the last purchase.
    if succeeded:
        last_purchase = max(a["resolved_at"] for a in succeeded)
        days_since = (datetime.utcnow() - last_purchase).days
        recency_score = clamp(1 - days_since / recency_window) if recency_window > 0 else 0.0
    else:
        last_purchase = None
        days_since = None
        recency_score = 0.0

    # 5. Responsiveness - do nudges actually work on this customer? This
    #    is the one component that directly predicts whether spending
    #    recovery effort here will pay off, which is what the tier is FOR.
    #    Neutral 0.5 when they've never been nudged: scoring them 0 would
    #    punish well-behaved customers for never having needed recovery.
    answered = counts["nudges_resumed"] + counts["nudges_ignored"]
    responsiveness_score = (counts["nudges_resumed"] / answered) if answered else 0.5

    score = round(
        ENGAGEMENT_WEIGHTS["completion"] * completion_score
        + ENGAGEMENT_WEIGHTS["frequency"] * frequency_score
        + ENGAGEMENT_WEIGHTS["monetary"] * monetary_score
        + ENGAGEMENT_WEIGHTS["recency"] * recency_score
        + ENGAGEMENT_WEIGHTS["responsiveness"] * responsiveness_score
    )

    return {
        "score": score,
        "components": {
            "completion": round(completion_score, 3),
            "frequency": round(frequency_score, 3),
            "monetary": round(monetary_score, 3),
            "recency": round(recency_score, 3),
            "responsiveness": round(responsiveness_score, 3),
        },
        "inputs": {
            "purchase_attempts": len(attempts),
            "successful_attempts": len(succeeded),
            "attributable_failed_attempts": len(attributable_failed),
            "explicit_cancels": counts["cancels"],
            "silent_abandons": counts["abandons"],
            "nudges_resumed": counts["nudges_resumed"],
            "nudges_ignored": counts["nudges_ignored"],
            "avg_order_value_inr": round(avg_value, 2),
            "days_since_last_purchase": days_since,
            "tenure_months": round(tenure_months, 1),
        },
    }


def _risk_gate(db: Session, customer_id: str, breakdown: dict, get) -> str | None:
    """RISK is an enforcement state, not the bottom rung - so it's checked
    BEFORE the score ladder and can't be reached by merely scoring badly.
    Returns a reason string if the customer trips it, else None.

    Three independent paths, deliberately kept separate: a permanent
    risk_block flag (fraud - never self-corrects), a customer-attributable
    failure rate (a card that keeps bouncing), and a cancel rate (someone
    who repeatedly gets to checkout and bails).
    """
    min_attempts = get("tier_risk_min_attempts", runtime_flags.get_tier_risk_min_attempts)
    max_fail_rate = get("tier_risk_attributable_failure_rate", runtime_flags.get_tier_risk_attributable_failure_rate)
    max_cancel_rate = get("tier_risk_cancel_rate", runtime_flags.get_tier_risk_cancel_rate)

    inputs = breakdown["inputs"]

    has_risk_block = db.query(Order.id).filter(
        Order.customer_id == customer_id,
        Order.failure_reason == FailureReason.RISK_BLOCK,
    ).first() is not None
    if has_risk_block:
        return "risk_block flag on a prior payment"

    attempts = inputs["purchase_attempts"]
    if attempts >= min_attempts:
        fail_rate = inputs["attributable_failed_attempts"] / attempts
        if fail_rate > max_fail_rate:
            return f"{fail_rate:.0%} of purchase attempts failed for customer-side reasons"

    cancel_intents = inputs["explicit_cancels"] + inputs["successful_attempts"]
    if cancel_intents >= min_attempts:
        cancel_rate = inputs["explicit_cancels"] / cancel_intents
        if cancel_rate > max_cancel_rate:
            return f"{cancel_rate:.0%} of checkout intents ended in an explicit cancel"

    return None


def tier_breakdown(db: Session, customer_id: str, overrides: dict | None = None) -> dict:
    """Everything behind a customer's tier in one call: the score, its
    components, the risk verdict, and how far they are from the next tier.
    compute_tier() is the thin wrapper that just takes the label.
    """
    overrides = overrides or {}

    def get(key, live_getter):
        return overrides[key] if key in overrides else live_getter()

    breakdown = engagement_score(db, customer_id, overrides=overrides)
    score = breakdown["score"]
    attempts = breakdown["inputs"]["purchase_attempts"]

    loyal_score = get("tier_loyal_score", runtime_flags.get_tier_loyal_score)
    regular_score = get("tier_regular_score", runtime_flags.get_tier_regular_score)
    min_loyal = get("tier_min_attempts_for_loyal", runtime_flags.get_tier_min_attempts_for_loyal)
    min_regular = get("tier_min_attempts_for_regular", runtime_flags.get_tier_min_attempts_for_regular)

    if attempts == 0:
        tier, reason = CustomerTier.NEW, "No completed purchase attempts yet"
    else:
        risk_reason = _risk_gate(db, customer_id, breakdown, get)
        if risk_reason:
            tier, reason = CustomerTier.RISK, risk_reason
        elif attempts >= min_loyal and score >= loyal_score:
            tier, reason = CustomerTier.LOYAL, f"Engagement score {score} with {attempts} purchase attempts"
        elif attempts >= min_regular and score >= regular_score:
            tier, reason = CustomerTier.REGULAR, f"Engagement score {score} with {attempts} purchase attempts"
        else:
            tier, reason = CustomerTier.CASUAL, f"Engagement score {score} with {attempts} purchase attempts"

    # What would it take to move up? Powers both the merchant's near-miss
    # lists and the customer-facing "how do I level up" view, from one
    # place so the two can never disagree.
    next_tier = None
    if tier == CustomerTier.CASUAL:
        next_tier = {
            "tier": CustomerTier.REGULAR.value,
            "score_needed": regular_score, "score_gap": max(0, regular_score - score),
            "attempts_needed": min_regular, "attempts_gap": max(0, min_regular - attempts),
        }
    elif tier == CustomerTier.REGULAR:
        next_tier = {
            "tier": CustomerTier.LOYAL.value,
            "score_needed": loyal_score, "score_gap": max(0, loyal_score - score),
            "attempts_needed": min_loyal, "attempts_gap": max(0, min_loyal - attempts),
        }

    return {**breakdown, "tier": tier, "reason": reason, "next_tier": next_tier}


def compute_tier(db: Session, customer_id: str, overrides: dict | None = None) -> CustomerTier:
    """
    overrides: optional dict of {param_name: value} to use INSTEAD of the
    live runtime_flags values, for exactly this one computation - nothing
    is read or written to runtime_flags when an override is given for that
    key. This is what lets a preview show "what would happen if this
    threshold were X" without setting X first - see
    /insights/tier-reevaluation-preview, the only caller that passes
    overrides. refresh_tier() (real, persisted changes) always calls this
    with overrides=None, so it only ever acts on live values.

    This matters MORE now than it did with the old thresholds: "the loyal
    cutoff moves from 70 to 65" is far less intuitive than "4 orders
    becomes 5", so seeing the exact customer-level diff first is the main
    safeguard against a well-meaning threshold change quietly re-tiering
    half the customer base.
    """
    return tier_breakdown(db, customer_id, overrides=overrides)["tier"]


def refresh_tier(db: Session, customer: Customer) -> Customer:
    """Recompute and persist the tier if it changed. Returns the (possibly
    updated) customer. Also logs the transition to the audit trail - this
    is the ONLY place a tier ever changes, so hooking it here covers
    everything. Before this hook existed, tier changes were silent: no
    record of when a customer moved tiers or what they moved from."""
    result = tier_breakdown(db, customer.id)
    new_tier = result["tier"]
    if new_tier != customer.tier:
        old_tier = customer.tier
        customer.tier = new_tier
        db.commit()
        db.refresh(customer)
        write_audit_entry(
            db,
            action_type="tier_changed",
            details={
                "customer_id": customer.id,
                "old_tier": old_tier.value if old_tier else None,
                "new_tier": new_tier.value,
                "engagement_score": result["score"],
                "reason": result["reason"],
            },
        )
    return customer


# ── Incentive sizing ───────────────────────────────────────────────────────

def _score_band(tier: CustomerTier) -> tuple[float, float]:
    """Each ladder tier's engagement-score span. Reuses the SAME
    thresholds that define tier membership rather than inventing separate
    numbers, so retuning a tier threshold automatically retunes the
    discount curve inside it - the two can't drift apart."""
    regular = runtime_flags.get_tier_regular_score()
    loyal = runtime_flags.get_tier_loyal_score()
    return {
        CustomerTier.CASUAL: (0.0, float(regular)),
        CustomerTier.REGULAR: (float(regular), float(loyal)),
        CustomerTier.LOYAL: (float(loyal), 100.0),
    }[tier]


def incentive_pct_for_customer(tier: CustomerTier, score: int) -> int:
    """Where a customer sits INSIDE their own tier's score band decides
    where they sit inside that tier's discount band. A customer at the top
    of REGULAR gets close to 20%; one who just scraped in gets close to
    10%. Fully deterministic - no LLM call on the cart-event hot path, no
    fallback to design, and the audit trail can always reproduce exactly
    why a given customer was offered a given number."""
    band = runtime_flags.get_incentive_pct_band(tier.value)
    if band is None:
        return 0
    pct_low, pct_high = band
    score_low, score_high = _score_band(tier)
    span = score_high - score_low
    position = clamp((score - score_low) / span) if span > 0 else 0.0
    return round(pct_low + position * (pct_high - pct_low))


def customer_stats(db: Session, customer_id: str) -> dict:
    """Summary used by the storefront 'my account' view and the merchant
    dashboard's customer table. Now carries the engagement breakdown, so
    both surfaces can explain a tier instead of just asserting it."""
    result = tier_breakdown(db, customer_id)
    attempts = purchase_attempts(db, customer_id)
    succeeded = [a for a in attempts if a["succeeded"]]

    raw_orders = db.query(Order).filter(
        Order.customer_id == customer_id,
        Order.status.in_([OrderStatus.CAPTURED, OrderStatus.FAILED]),
    ).all()
    failure_reasons = Counter(o.failure_reason.value for o in raw_orders if o.failure_reason)

    return {
        # Purchase-attempt level (retries collapsed) - the honest numbers.
        "total_orders": len(attempts),
        "successful_orders": len(succeeded),
        "failed_orders": len(attempts) - len(succeeded),
        # Raw try-level counts, kept separate and clearly named so the
        # difference between "tried 7 times" and "bought 5 things" stays
        # visible instead of being silently averaged together.
        "total_payment_attempts": len(raw_orders),
        "engagement_score": result["score"],
        "score_components": result["components"],
        "tier": result["tier"].value,
        "tier_reason": result["reason"],
        "next_tier": result["next_tier"],
        "most_common_failure_reason": failure_reasons.most_common(1)[0][0] if failure_reasons else None,
    }

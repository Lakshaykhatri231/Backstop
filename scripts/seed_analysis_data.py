"""
Seeds demo data for all three merchant-facing policy analyses - Incentive
Analysis, Loss & Recovery (payment-failure/checkout-dropoff) Analysis, and
Tier Analysis - from one script and one command. Previously three separate
files (seed_incentive_analysis_data.py, seed_payment_failure_analysis_data.py,
seed_tier_analysis_data.py); collapsed into one so there's a single place to
run and maintain analysis demo data from. Each analysis still gets its own
dedicated section below with its own hand-built scenarios - that's still
necessary (randomized volume from one analysis's data doesn't reliably
surface another analysis's specific edge cases), it's just no longer three
separate commands or three separate customer-naming schemes.

Writes directly to the DB (not through the HTTP/webhook API), so outcomes
and timestamps can be set exactly rather than replayed through the live
decision ladder. Safe to re-run: every customer gets a real name drawn from
one shared, deterministically-shuffled pool (see NAMES below), so a second
run draws the exact same names in the exact same order and finds each one
already exists rather than creating duplicates. Doesn't touch the 5 named
demo accounts from seed_storefront_customers.py - those live under
@demo.com, everything here lives under @seed.demo.

Usage:
    python scripts/seed_analysis_data.py

Requires DATABASE_URL to be set (.env) - does NOT require the server to be
running.
"""
import json
import random
import sys
import uuid
from collections import Counter
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
from app.models import (
    Customer, Order, OrderStatus, Event, EventType, Decision,
    CartEvent, CartEventType, CartEventStatus, FailureReason, CustomerTier, AgentAction,
)
from app.auth import hash_password
from app.tiering import refresh_tier
from app import runtime_flags

DEMO_PASSWORD = "password123"


def days_ago(n: int, hour: int = 12) -> datetime:
    return datetime.utcnow() - timedelta(days=n, hours=-hour)


# ── Real names, shared across every section below ──────────────────────────
# One pool for the whole file so every seeded customer - regardless of which
# analysis's scenario created them - gets a distinct real name; nothing here
# is a "Seed Loyal 00"-style label. Drawn from its own dedicated Random
# instance (seed 101) so it never interacts with each section's own
# random.seed() call below - those control amounts/outcomes/day-offsets and
# must stay independently reproducible, exactly as they were as three
# separate scripts.
FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Reyansh", "Krishna", "Ishaan", "Rohan", "Karthik", "Siddharth",
    "Ananya", "Diya", "Ishita", "Saanvi", "Aadhya", "Kavya", "Myra", "Anika", "Riya", "Meera",
    "Rahul", "Karan", "Nikhil", "Varun", "Aryan", "Dhruv", "Kabir", "Yash", "Vikram", "Aman",
    "Neha", "Pooja", "Sneha", "Tanvi", "Isha", "Simran", "Aditi", "Bhavya", "Nisha", "Divya",
    "Rohit", "Amit", "Sanjay", "Manish", "Deepak", "Rajesh", "Suresh", "Ravi", "Ajay", "Gaurav",
    "Fatima", "Ayesha", "Sara", "Imran", "Zoya", "Farhan", "Ali", "Sameer",
    "Lakshmi", "Kavitha", "Deepika", "Anjali", "Shreya", "Pallavi", "Swati",
    "Arnav", "Kunal", "Nitin", "Harsh", "Abhishek", "Vishal", "Tarun",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Mehta", "Patel", "Shah", "Iyer", "Nair", "Menon", "Rao",
    "Reddy", "Kumar", "Singh", "Malhotra", "Kapoor", "Chopra", "Bhatt", "Joshi", "Desai", "Agarwal",
    "Bose", "Chatterjee", "Mukherjee", "Banerjee", "Pillai", "Krishnan", "Nambiar", "Rana", "Chauhan", "Bansal",
    "Khan", "Ahmed", "Siddiqui", "Ansari", "D'Souza", "Fernandes", "Pinto", "Rodrigues",
]
# Already used by seed_storefront_customers.py's 5 named demo accounts
# (different email domain, @demo.com - excluded here anyway so no dashboard
# listing ever shows the same full name for two unrelated customers).
_RESERVED_NAMES = {"Priya Sharma", "Arjun Kapoor", "Neha Verma", "Rahul Iyer", "Zara Khan"}


def _build_name_pool():
    rng = random.Random(101)
    combos = [f"{first} {last}" for first in FIRST_NAMES for last in LAST_NAMES]
    combos = [c for c in combos if c not in _RESERVED_NAMES]
    rng.shuffle(combos)
    return iter(combos)


NAMES = _build_name_pool()


def next_identity() -> tuple[str, str]:
    """One (name, email) pair, never repeated within a run. Draw order is
    fixed by the dedicated rng above, so re-running the script draws the
    same identities in the same order every time - which is what makes
    every make_*_customer() below safe to re-run without duplicating rows."""
    name = next(NAMES)
    email = f"{name.lower().replace(' ', '.')}@seed.demo"
    return name, email


# ============================================================================
# Section 1: Incentive Analysis
# ============================================================================
# Enough volume per tier x trigger bucket to clear the low-sample threshold
# (10), plus deliberate edge cases so freq-cap blocking, amount-cap
# blocking, tier-gate blocking, and the low-sample flag itself all have
# something real to show - not just the happy path.

def _ia_tier_incentive_pct(tier: CustomerTier) -> int:
    """Midpoint of this tier's CURRENT discount band - not a flat rate.
    Bands are per-tier now (casual 0-10%, regular 10-20%, loyal 20-30% by
    default); a hardcoded flat rate used to seed every tier at the same 5%,
    which made Incentive Analysis's avg-discount-vs-band metric look broken
    (e.g. loyal showing ~5% against a 20-30% band)."""
    band = runtime_flags.get_incentive_pct_band(tier.value)
    return round((band[0] + band[1]) / 2) if band else 5


def _ia_make_customer(db, tier):
    name, email = next_identity()
    existing = db.query(Customer).filter(Customer.email == email).first()
    if existing:
        return existing
    customer = Customer(
        email=email, password_hash=hash_password(DEMO_PASSWORD), name=name,
        tier=tier, created_at=days_ago(90),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _ia_seed_event(db, customer, event_type, tier, amount_inr,
                    amount_cap_ok, freq_cap_ok, tier_incentive_eligible,
                    outcome, days_back):
    """
    outcome: "resumed" | "declined" | "expired" | "converted_no_incentive"
             | "not_converted"
    Mirrors what rule_based_cart_event_decision would produce for these
    flags, without calling it directly.
    """
    incentive_ok = tier_incentive_eligible and amount_cap_ok and freq_cap_ok

    if incentive_ok:
        action = AgentAction.OFFER_INCENTIVE
        incentive_pct = _ia_tier_incentive_pct(tier)
        final_amt = round(amount_inr * (1 - incentive_pct / 100), 2)
    else:
        action = (AgentAction.SEND_RESUME_LINK if event_type == CartEventType.EXPLICIT_CANCEL
                  else AgentAction.SEND_REMINDER)
        incentive_pct = None
        final_amt = None

    status = None
    resolved_at = None
    if action == AgentAction.OFFER_INCENTIVE or (
        event_type == CartEventType.EXPLICIT_CANCEL and action == AgentAction.SEND_RESUME_LINK
    ):
        status = {
            "resumed": CartEventStatus.RESUMED,
            "declined": CartEventStatus.DECLINED,
            "expired": CartEventStatus.EXPIRED,
        }.get(outcome, CartEventStatus.PENDING)
        if status in (CartEventStatus.RESUMED, CartEventStatus.DECLINED):
            resolved_at = days_ago(max(days_back - 1, 0))

    reasoning = (
        f"Seeded {tier.value}/{event_type.value} scenario for incentive-analysis "
        f"demo data (outcome={outcome})."
    )

    ce = CartEvent(
        customer_id=customer.id, event_type=event_type, items_json="[]",
        amount_inr=amount_inr, tier_at_time=tier, action=action,
        confidence=0.75, reasoning=reasoning, outcome="logged",
        status=status,
        expires_at=days_ago(max(days_back - 1, 0)) if status else None,
        resolved_at=resolved_at,
        incentive_pct=incentive_pct, incentive_final_amount_inr=final_amt,
        amount_cap_ok=amount_cap_ok, freq_cap_ok=freq_cap_ok,
        tier_incentive_eligible=tier_incentive_eligible,
        created_at=days_ago(days_back),
    )
    db.add(ce)
    db.commit()
    db.refresh(ce)

    converted = outcome in ("resumed", "converted_no_incentive")
    if converted:
        captured_amount = final_amt if final_amt is not None else amount_inr
        order = Order(
            # Seeded history is settled history: never an open revenue/risk item.
            revenue_recorded=True, risk_settled=True,
            customer_id=customer.id,
            razorpay_order_id=f"order_seed_ia_{customer.email.split('@')[0]}_{random.randint(10000, 99999)}",
            items_json="[]", amount_inr=captured_amount, status=OrderStatus.CAPTURED,
            recovered_from_cart_event_id=ce.id,
            created_at=days_ago(max(days_back - 1, 0)),
            resolved_at=days_ago(max(days_back - 1, 0)),
        )
        db.add(order)
        db.commit()
    return ce


def _ia_seed_bucket(db, tier, event_type, n, tier_incentive_eligible) -> list[Customer]:
    """Generates n distinct single-event customers for one (tier, event_type)
    bucket, with a realistic-ish outcome mix, spread over the last 34 days."""
    outcomes_cycle = ["resumed", "resumed", "resumed", "declined", "declined", "expired",
                       "converted_no_incentive", "not_converted"]
    customers = []
    for i in range(n):
        customer = _ia_make_customer(db, tier)
        customers.append(customer)
        amount_inr = random.choice([799, 1299, 1499, 1799, 1899])
        outcome = outcomes_cycle[i % len(outcomes_cycle)]
        days_back = random.randint(1, 34)
        # amount_cap_ok mirrors the real gate exactly (amount vs. THIS
        # tier's live cap) rather than a blanket True - matters for
        # risk/new, whose cap is a 0 placeholder (they're excluded via
        # tier_incentive_eligible instead, never via this cap). freq_cap_ok
        # =True is genuinely correct as a constant: every seeded customer
        # here is fresh with exactly one event, so there is no prior
        # 30-day incentive history to be capped by, for any tier.
        amount_cap_ok = amount_inr <= runtime_flags.get_incentive_max_order_value(tier.value)
        _ia_seed_event(
            db, customer, event_type, tier, amount_inr,
            amount_cap_ok=amount_cap_ok, freq_cap_ok=True,
            tier_incentive_eligible=tier_incentive_eligible,
            outcome=outcome, days_back=days_back,
        )
    return customers


def _ia_seed_edge_cases(db) -> list[Customer]:
    """Deliberate scenarios so every metric the analysis computes has a
    real, non-zero example to show - not just the happy-path buckets."""
    customers = []

    # Amount-cap-blocked: order value above the LOYAL tier's live amount
    # cap (runtime_flags, not a hardcoded number), tier otherwise
    # incentive-eligible.
    above_loyal_cap = runtime_flags.get_incentive_max_order_value("loyal") + 500
    for i in range(3):
        c = _ia_make_customer(db, CustomerTier.LOYAL)
        customers.append(c)
        _ia_seed_event(
            db, c, CartEventType.SILENT_ABANDON, CustomerTier.LOYAL,
            amount_inr=above_loyal_cap, amount_cap_ok=False, freq_cap_ok=True,
            tier_incentive_eligible=True,
            outcome="converted_no_incentive" if i == 0 else "not_converted",
            days_back=6 + i,
        )

    # Freq-cap-blocked: same customer, two events inside 30 days - first
    # gets the incentive, second is blocked by the frequency cap.
    for i in range(3):
        c = _ia_make_customer(db, CustomerTier.LOYAL)
        customers.append(c)
        _ia_seed_event(
            db, c, CartEventType.EXPLICIT_CANCEL, CustomerTier.LOYAL,
            amount_inr=1499, amount_cap_ok=True, freq_cap_ok=True,
            tier_incentive_eligible=True, outcome="resumed", days_back=22,
        )
        _ia_seed_event(
            db, c, CartEventType.EXPLICIT_CANCEL, CustomerTier.LOYAL,
            amount_inr=1699, amount_cap_ok=True, freq_cap_ok=False,
            tier_incentive_eligible=True,
            outcome="converted_no_incentive" if i == 0 else "not_converted",
            days_back=4,
        )
    return customers


def seed_incentive_analysis(db) -> list[Customer]:
    random.seed(7)
    print("Seeding incentive-analysis demo data...")

    customers: list[Customer] = []
    # Loyal and casual each get enough volume (>= 10) to clear the
    # low-sample threshold on their own. Casual is seeded with
    # tier_incentive_eligible=False throughout - NOT because that matches
    # today's default (casual IS incentive-eligible by default; this is a
    # deliberate synthetic "what if it weren't" scenario) - so the
    # tier-gate-blocked metric has a real, non-zero example to show.
    customers += _ia_seed_bucket(db, CustomerTier.LOYAL, CartEventType.SILENT_ABANDON, 12, True)
    customers += _ia_seed_bucket(db, CustomerTier.LOYAL, CartEventType.EXPLICIT_CANCEL, 12, True)
    customers += _ia_seed_bucket(db, CustomerTier.CASUAL, CartEventType.SILENT_ABANDON, 14, False)
    customers += _ia_seed_bucket(db, CustomerTier.CASUAL, CartEventType.EXPLICIT_CANCEL, 13, False)
    # Risk and new: deliberately small - both realistically see less
    # engagement, and this keeps at least one bucket genuinely low-sample.
    customers += _ia_seed_bucket(db, CustomerTier.RISK, CartEventType.SILENT_ABANDON, 4, False)
    customers += _ia_seed_bucket(db, CustomerTier.NEW, CartEventType.SILENT_ABANDON, 5, False)
    customers += _ia_seed_edge_cases(db)

    # CartEvent.tier_at_time is a historical snapshot and stays untouched -
    # that's what the analysis buckets on. This only reconciles each
    # customer's CURRENT tier, which the maintenance sweep would otherwise
    # recompute (and visibly change) a few minutes into a demo.
    for c in customers:
        refresh_tier(db, c)
    distribution = Counter(c.tier.value for c in customers)
    print(f"  -> {len(customers)} customers, tier distribution {dict(sorted(distribution.items()))}")
    return customers


# ============================================================================
# Section 2: Loss & Recovery (payment-failure / checkout-dropoff) Analysis
# ============================================================================
# Events, Decisions, and matching Orders that exercise every metric the
# Loss & Recovery Analysis computes: resolved and unresolved failure
# streaks, confidence-gate overrides, high-value escalations, repeat
# offenders, and dropoff-side equivalents.

# How much clean purchase history each tier needs before its failure
# streaks get layered on top. Tier is an earned engagement score, not a
# label you can just assert on the row - a customer holding nothing except
# failures doesn't compute as LOYAL, so without a baseline the seeded label
# and the real tier disagree the moment the maintenance sweep recomputes it.
PF_BASELINE_PURCHASES = {
    CustomerTier.LOYAL: 7,      # clears the 5-attempt bar with room for failures
    CustomerTier.CASUAL: 1,
    CustomerTier.RISK: 0,       # earns RISK from its attributable failures
    CustomerTier.NEW: 0,        # must stay at zero attempts to remain NEW
}

PF_FAILURE_REASONS = [
    FailureReason.UNKNOWN, FailureReason.INVALID_CARD, FailureReason.CARD_EXPIRED,
    FailureReason.CANCELLED, FailureReason.INSUFFICIENT_FUNDS, FailureReason.BANK_DECLINE,
    FailureReason.NETWORK_ERROR, FailureReason.AUTHENTICATION_FAILED,
]

PF_TIERS = [CustomerTier.LOYAL, CustomerTier.CASUAL, CustomerTier.RISK, CustomerTier.NEW]


def _pf_rzp_id(prefix="order"):
    return f"{prefix}_seed_{uuid.uuid4().hex[:12]}"


def _pf_make_order(db, customer, items, status, failure_reason, amount_inr, days_back):
    o = Order(
        revenue_recorded=True, risk_settled=True,
        customer_id=customer.id, razorpay_order_id=_pf_rzp_id(), items_json=json.dumps(items),
        amount_inr=amount_inr, status=status, failure_reason=failure_reason,
        created_at=days_ago(days_back),
        resolved_at=days_ago(max(days_back - 1, 0)) if status != OrderStatus.CREATED else None,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _pf_seed_baseline_history(db, customer, tier):
    for j in range(PF_BASELINE_PURCHASES.get(tier, 0)):
        _pf_make_order(
            db, customer, [{"sku": f"baseline-{j}", "qty": 1}], OrderStatus.CAPTURED,
            None, random.choice([1499, 1899, 2499]), days_back=60 - j * 3,
        )


def _pf_make_customer(db, tier):
    name, email = next_identity()
    existing = db.query(Customer).filter(Customer.email == email).first()
    if existing:
        return existing
    c = Customer(email=email, password_hash=hash_password(DEMO_PASSWORD), name=name,
                 tier=tier, created_at=days_ago(90))
    db.add(c)
    db.commit()
    db.refresh(c)
    _pf_seed_baseline_history(db, c, tier)
    return c


def _pf_make_event_decision(db, order, failure_reason, attempt_count, action, confidence,
                             escalated_by_gate, source, days_back, amount_override=None,
                             override_action=None, amount_triggered=False):
    amount_inr = amount_override if amount_override is not None else order.amount_inr
    ev = Event(
        event_type=EventType.PAYMENT_FAILED, customer_id=f"rzp_cust_{order.customer_id[:8]}",
        razorpay_order_id=order.razorpay_order_id, amount_inr=amount_inr,
        failure_reason=failure_reason, attempt_count=attempt_count,
        received_at=days_ago(days_back),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)

    final_action = "escalate_to_human" if (escalated_by_gate or amount_triggered) else action
    if escalated_by_gate:
        reasoning = (
            f"[Confidence gate override] Original decision was '{override_action or action}' "
            f"at confidence {confidence:.2f}, below threshold 0.70. Forced to escalate. "
            f"Reasoning for {failure_reason.value}, attempt {attempt_count}."
        )
    elif amount_triggered:
        reasoning = f"Amount ₹{amount_inr:.0f} is above the high-value threshold (₹5000); routing to human approval."
    else:
        reasoning = f"Rules ladder: {failure_reason.value}, attempt {attempt_count} -> {action}."

    d = Decision(
        event_id=ev.id, action=final_action, confidence=confidence, reasoning=reasoning,
        source=source, escalated=escalated_by_gate, executed=True, outcome="logged",
    )
    db.add(d)
    db.commit()
    return ev, d


def _pf_make_dropoff_event_decision(db, customer, amount_inr, action, confidence,
                                     escalated_by_gate, source, days_back, override_action=None,
                                     amount_triggered=False):
    order_id = _pf_rzp_id("dropoff")
    final_action = "escalate_to_human" if (escalated_by_gate or amount_triggered) else action
    ev = Event(
        event_type=EventType.CHECKOUT_ABANDONED, customer_id=f"rzp_cust_{customer.id[:8]}",
        razorpay_order_id=order_id, amount_inr=amount_inr, checkout_status="attempted",
        minutes_since_created=random.randint(10, 120), abandonment_count=random.randint(1, 3),
        received_at=days_ago(days_back),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)

    if escalated_by_gate:
        reasoning = (
            f"[Confidence gate override] Original decision was '{override_action or action}' "
            f"at confidence {confidence:.2f}, below threshold 0.70. Forced to escalate."
        )
    elif amount_triggered:
        reasoning = f"High-value order (₹{amount_inr:.0f} >= ₹5000). Routing to human review regardless of abandonment count."
    else:
        reasoning = f"Dropoff ladder decision -> {action}."

    d = Decision(
        event_id=ev.id, action=final_action, confidence=confidence, reasoning=reasoning,
        source=source, escalated=escalated_by_gate, executed=True, outcome="logged",
    )
    db.add(d)
    db.commit()
    return ev, d


def _pf_seed_resolved_streak(db, customer, reason, days_back, attempts_to_resolve, source="llm"):
    """A customer who fails `attempts_to_resolve - 1` times then succeeds."""
    items = [{"sku": "wireless-earbuds", "qty": 1}]
    amount = random.choice([799, 1299, 1499, 1899])
    for attempt in range(1, attempts_to_resolve):
        o = _pf_make_order(db, customer, items, OrderStatus.FAILED, reason, amount,
                            days_back=days_back - attempt + attempts_to_resolve)
        action = "retry_now" if attempt == 1 else "retry_later"
        _pf_make_event_decision(db, o, reason, attempt, action, confidence=round(random.uniform(0.72, 0.9), 2),
                                 escalated_by_gate=False, source=source, days_back=o.created_at and days_back)
    # final successful order
    _pf_make_order(db, customer, items, OrderStatus.CAPTURED, None, amount, days_back=max(days_back - attempts_to_resolve, 0))


def _pf_seed_unresolved_streak(db, customer, reason, days_back, attempts, source="llm",
                                escalate_at_cap=False, gate_escalate_last=False):
    """A customer who fails repeatedly and never recovers."""
    items = [{"sku": "mechanical-keyboard", "qty": 1}]
    amount = random.choice([1999, 3999, 4999])
    for attempt in range(1, attempts + 1):
        o = _pf_make_order(db, customer, items, OrderStatus.FAILED, reason, amount, days_back=days_back - attempt + attempts)
        is_last = attempt == attempts
        if is_last and escalate_at_cap:
            _pf_make_event_decision(db, o, reason, attempt, "escalate_to_human", confidence=0.95,
                                     escalated_by_gate=False, source=source, days_back=days_back)
        elif is_last and gate_escalate_last:
            _pf_make_event_decision(db, o, reason, attempt, "retry_later", confidence=round(random.uniform(0.4, 0.65), 2),
                                     escalated_by_gate=True, source=source, days_back=days_back,
                                     override_action="retry_later")
        else:
            action = "retry_now" if attempt == 1 else "retry_later"
            _pf_make_event_decision(db, o, reason, attempt, action, confidence=round(random.uniform(0.72, 0.9), 2),
                                     escalated_by_gate=False, source=source, days_back=days_back)


def seed_payment_failure_analysis(db) -> list[Customer]:
    random.seed(11)
    print("Seeding payment-failure / dropoff analysis demo data...")

    customers_by_tier: dict[CustomerTier, list[Customer]] = {}
    for tier in PF_TIERS:
        for i in range(15):
            customers_by_tier.setdefault(tier, []).append(_pf_make_customer(db, tier))

    # Resolved streaks (1 or 2 attempts), spread across reasons/tiers
    for i in range(20):
        tier = random.choice(PF_TIERS)
        cust = customers_by_tier[tier][i % 15]
        reason = random.choice(PF_FAILURE_REASONS)
        _pf_seed_resolved_streak(db, cust, reason, days_back=random.randint(2, 34),
                                  attempts_to_resolve=random.choice([1, 2]),
                                  source=random.choice(["llm", "llm", "llm", "rules_engine_fallback"]))

    # Unresolved streaks that hit the attempt cap (4th attempt -> escalate)
    for i in range(8):
        tier = random.choice(PF_TIERS)
        cust = customers_by_tier[tier][(i + 5) % 15]
        reason = random.choice([FailureReason.BANK_DECLINE, FailureReason.NETWORK_ERROR, FailureReason.UNKNOWN])
        _pf_seed_unresolved_streak(db, cust, reason, days_back=random.randint(2, 30), attempts=4,
                                    escalate_at_cap=True, source="llm")

    # Unresolved streaks where the LAST attempt gets gate-force-escalated
    # (low confidence) - real material for confidence_override_rates.
    for i in range(6):
        tier = random.choice(PF_TIERS)
        cust = customers_by_tier[tier][(i + 3) % 15]
        reason = random.choice(PF_FAILURE_REASONS)
        _pf_seed_unresolved_streak(db, cust, reason, days_back=random.randint(2, 25), attempts=random.choice([1, 2]),
                                    gate_escalate_last=True, source="llm")

    # Pure amount-triggered escalations (high-value floor, nothing else wrong)
    for i in range(5):
        tier = random.choice(PF_TIERS)
        cust = customers_by_tier[tier][(i + 1) % 15]
        items = [{"sku": "espresso-machine", "qty": 1}]
        amount = random.choice([5500, 6200, 9999, 15000])
        o = _pf_make_order(db, cust, items, OrderStatus.FAILED, FailureReason.UNKNOWN, amount, days_back=random.randint(1, 20))
        _pf_make_event_decision(db, o, FailureReason.UNKNOWN, 1, "escalate_to_human", confidence=0.60,
                                 escalated_by_gate=False, source="llm", days_back=o.created_at and 5,
                                 amount_override=amount, amount_triggered=True)

    # risk_block escalations - always escalate, no exceptions
    for i in range(4):
        tier = random.choice(PF_TIERS)
        cust = customers_by_tier[tier][(i + 7) % 15]
        items = [{"sku": "smart-watch", "qty": 1}]
        amount = random.choice([1499, 2999])
        o = _pf_make_order(db, cust, items, OrderStatus.FAILED, FailureReason.RISK_BLOCK, amount, days_back=random.randint(1, 15))
        _pf_make_event_decision(db, o, FailureReason.RISK_BLOCK, 1, "escalate_to_human", confidence=0.85,
                                 escalated_by_gate=False, source="llm", days_back=5)

    # Repeat offenders: customers with 2+ distinct failed-purchase streaks
    for i in range(4):
        tier = random.choice([CustomerTier.RISK, CustomerTier.CASUAL])
        cust = customers_by_tier[tier][(i + 9) % 15]
        _pf_seed_unresolved_streak(db, cust, FailureReason.BANK_DECLINE, days_back=25, attempts=2, source="llm")
        _pf_seed_unresolved_streak(db, cust, FailureReason.CARD_EXPIRED, days_back=10, attempts=1, source="llm")

    # Dropoff (poller) events - mix of resolved-looking ranges, escalations,
    # gate overrides, and a pure amount-triggered one.
    dropoff_customers = customers_by_tier[CustomerTier.LOYAL] + customers_by_tier[CustomerTier.CASUAL]
    for i in range(14):
        cust = dropoff_customers[i % len(dropoff_customers)]
        amount = random.choice([699, 999, 1499, 2499])
        _pf_make_dropoff_event_decision(db, cust, amount, "send_reminder", confidence=round(random.uniform(0.75, 0.9), 2),
                                         escalated_by_gate=False, source=random.choice(["llm", "llm", "rules_engine_fallback"]),
                                         days_back=random.randint(1, 28))
    for i in range(5):
        cust = dropoff_customers[(i + 2) % len(dropoff_customers)]
        _pf_make_dropoff_event_decision(db, cust, random.choice([999, 1499]), "offer_incentive",
                                         confidence=round(random.uniform(0.45, 0.65), 2),
                                         escalated_by_gate=True, source="llm", days_back=random.randint(1, 20),
                                         override_action="offer_incentive")
    for i in range(3):
        cust = dropoff_customers[(i + 4) % len(dropoff_customers)]
        amount = random.choice([6000, 8500])
        _pf_make_dropoff_event_decision(db, cust, amount, "escalate_to_human", confidence=0.85,
                                         escalated_by_gate=False, source="llm", days_back=random.randint(1, 15),
                                         amount_triggered=True)

    # Reconcile every seeded label against what the engagement score
    # actually produces. The failure streaks above are layered on at
    # random, so a customer can end up genuinely earning a different tier
    # than the one they were created with.
    customers = [c for tier_customers in customers_by_tier.values() for c in tier_customers]
    changed = 0
    for c in customers:
        before = c.tier
        refresh_tier(db, c)
        if c.tier != before:
            changed += 1
    distribution = Counter(c.tier.value for c in customers)
    print(f"  -> {len(customers)} customers ({changed} tier(s) reconciled), "
          f"tier distribution {dict(sorted(distribution.items()))}")
    return customers


# ============================================================================
# Section 3: Tier Analysis
# ============================================================================
# Near-miss customers on both boundaries, serial cart-abandoners whose
# completion rate holds them under the Loyal cutoff, risk-flagged customers
# who have/haven't paid since, dynamic-path risk customers, and dormant
# accounts spread across tiers. Tier is set to match what compute_tier()
# would actually produce for the history created, so the maintenance
# sweep (which recomputes every customer every 30 minutes) doesn't flip a
# seeded label mid-demo.

def _tier_make_customer(db, tier):
    name, email = next_identity()
    existing = db.query(Customer).filter(Customer.email == email).first()
    if existing:
        return existing
    c = Customer(email=email, password_hash=hash_password(DEMO_PASSWORD), name=name,
                 tier=tier, created_at=days_ago(90))
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _tier_make_order(db, customer, status, amount, days_back, failure_reason=None, sku="wireless-earbuds"):
    """sku matters: purchase_attempts() groups orders by their item
    signature, so consecutive failures on the SAME basket collapse into one
    attempt (the retry fix). Scenarios that need N separate failed attempts
    must vary the sku, or they'll all count as one."""
    o = Order(
        revenue_recorded=True, risk_settled=True,
        customer_id=customer.id, razorpay_order_id=f"order_seed_tier_{customer.email.split('@')[0]}_{random.randint(10000, 99999)}",
        items_json=json.dumps([{"sku": sku, "qty": 1}]), amount_inr=amount,
        status=status, failure_reason=failure_reason,
        created_at=days_ago(days_back), resolved_at=days_ago(max(days_back - 1, 0)) if status != OrderStatus.CREATED else None,
    )
    db.add(o)
    db.commit()
    return o


def _tier_make_cart_event(db, customer, event_type, days_back):
    ce = CartEvent(
        customer_id=customer.id, event_type=event_type, items_json="[]",
        amount_inr=random.choice([799, 1299]), tier_at_time=customer.tier, action="no_action",
        confidence=0.6, reasoning="Seeded for tier-analysis demo data.",
        created_at=days_ago(days_back),
    )
    db.add(ce)
    db.commit()


def _tier_make_cancel(db, customer, days_back):
    _tier_make_cart_event(db, customer, CartEventType.EXPLICIT_CANCEL, days_back)


def _tier_make_abandon(db, customer, days_back):
    _tier_make_cart_event(db, customer, CartEventType.SILENT_ABANDON, days_back)


def seed_tier_analysis(db) -> list[Customer]:
    random.seed(19)
    print("Seeding tier-analysis demo data...")
    customers: list[Customer] = []

    # --- Near-loyal: 4 successful purchases, strong score, but LOYAL needs
    # 5 purchase attempts - so they miss on VOLUME, not on score.
    for i in range(6):
        c = _tier_make_customer(db, CustomerTier.REGULAR)
        customers.append(c)
        for j in range(4):
            _tier_make_order(db, c, OrderStatus.CAPTURED, random.choice([1299, 1499, 1899]), days_back=15 - j)

    # --- Near-risk: standard tier, 1 order, failed - one more failure
    # would push them into risk.
    for i in range(5):
        c = _tier_make_customer(db, CustomerTier.CASUAL)
        customers.append(c)
        reason = random.choice(["bank_decline", "unknown", "network_error"])
        _tier_make_order(db, c, OrderStatus.FAILED, random.choice([799, 1499]), days_back=8, failure_reason=reason)

    # --- Serial abandoner: 5 completed purchases (past the LOYAL volume
    # bar) but 10 abandoned carts, so completion rate drags the score just
    # under the LOYAL cutoff.
    for i in range(4):
        c = _tier_make_customer(db, CustomerTier.REGULAR)
        customers.append(c)
        for j in range(5):
            _tier_make_order(db, c, OrderStatus.CAPTURED, random.choice([999, 1499, 1999]), days_back=20 - j * 2)
        for j in range(10):
            _tier_make_abandon(db, c, days_back=random.randint(3, 25))

    # --- Genuinely loyal: 5+ completed purchases at good value, recent, no
    # cancels - clears both the score bar (70) and the volume bar (5
    # attempts). Real loyal-tier volume for tier_wise_performance /
    # tier_distribution.
    for i in range(8):
        c = _tier_make_customer(db, CustomerTier.LOYAL)
        customers.append(c)
        n = random.choice([5, 6, 7])
        for j in range(n):
            _tier_make_order(db, c, OrderStatus.CAPTURED, random.choice([1299, 1899, 2499]), days_back=25 - j * 2)

    # --- Risk via risk_block, redeemed since (paid fine several times
    # after the flag) - the "were we too harsh" evidence.
    for i in range(5):
        c = _tier_make_customer(db, CustomerTier.RISK)
        customers.append(c)
        _tier_make_order(db, c, OrderStatus.FAILED, 1499, days_back=25, failure_reason="risk_block")
        for j in range(random.choice([2, 3, 4])):
            _tier_make_order(db, c, OrderStatus.CAPTURED, random.choice([999, 1499]), days_back=15 - j * 3)

    # --- Risk via risk_block, never redeemed - the contrast case.
    for i in range(3):
        c = _tier_make_customer(db, CustomerTier.RISK)
        customers.append(c)
        _tier_make_order(db, c, OrderStatus.FAILED, random.choice([999, 1499]), days_back=random.randint(5, 20), failure_reason="risk_block")

    # --- Risk via the dynamic path (not a permanent flag) - for contrast
    # against the risk_block cases above. Failure reasons must be
    # customer-ATTRIBUTABLE (bank_decline/unknown are the gateway's or
    # issuer's problem, excluded from tiering), and each failure must be a
    # DISTINCT basket (consecutive failures on the same items collapse
    # into one purchase attempt).
    for i in range(3):
        c = _tier_make_customer(db, CustomerTier.RISK)
        customers.append(c)
        _tier_make_order(db, c, OrderStatus.CAPTURED, 999, days_back=20, sku="wireless-earbuds")
        for j in range(3):
            _tier_make_order(db, c, OrderStatus.FAILED, random.choice([999, 1499]), days_back=14 - j * 4,
                              failure_reason=random.choice(["card_expired", "insufficient_funds", "invalid_card"]),
                              sku=f"risk-basket-{j}")

    # --- Dormant accounts: real orders, but all well outside any normal
    # range (7d/30d), spread across tiers. Order counts must clear each
    # tier's minimum-attempt bar, or the seeded label and the computed
    # tier disagree the moment the maintenance sweep runs.
    for tier, order_count in [
        (CustomerTier.LOYAL, 5),
        (CustomerTier.CASUAL, 2),
        (CustomerTier.RISK, 2),
    ]:
        for i in range(3):
            c = _tier_make_customer(db, tier)
            customers.append(c)
            for j in range(order_count):
                status = OrderStatus.CAPTURED if tier != CustomerTier.RISK else OrderStatus.FAILED
                fr = "risk_block" if tier == CustomerTier.RISK else None
                _tier_make_order(db, c, status, random.choice([1299, 1899]), days_back=55 + j * 2, failure_reason=fr)

    # --- New tier: signed up, never ordered.
    for i in range(5):
        c = _tier_make_customer(db, CustomerTier.NEW)
        customers.append(c)

    # Every scenario above is hand-built to land on a specific tier, so
    # this pass should change nothing - any count above zero is a scenario
    # that drifted out of sync with the thresholds.
    changed = 0
    for c in customers:
        before = c.tier
        refresh_tier(db, c)
        if c.tier != before:
            changed += 1
    distribution = Counter(c.tier.value for c in customers)
    print(f"  -> {len(customers)} customers ({changed} tier(s) reconciled), "
          f"tier distribution {dict(sorted(distribution.items()))}")
    return customers


# ============================================================================
def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    print("Seeding demo data for all three analyses (Incentive, Loss & Recovery, Tier)...\n")
    incentive_customers = seed_incentive_analysis(db)
    pf_customers = seed_payment_failure_analysis(db)
    tier_customers = seed_tier_analysis(db)

    db.close()

    total = len(incentive_customers) + len(pf_customers) + len(tier_customers)
    print(f"\nDone. Seeded {total} customers total, each with a real name under @seed.demo.")
    print(f"All seed accounts use password: {DEMO_PASSWORD}")
    print("Open the dashboard at http://localhost:8000/dashboard -> Audit Log, then:")
    print("  - Incentive Analysis: range=30d or range=all for volume; range=7d to see low-sample kick in")
    print("  - Loss & Recovery Analysis: range=30d or range=all for volume; range=7d for low-sample buckets")
    print("  - Tier Analysis: near-miss customers, dormant accounts and risk-flag redemption are all live")


if __name__ == "__main__":
    seed()

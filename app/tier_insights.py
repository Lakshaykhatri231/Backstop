"""
Pure aggregation layer for the tier-threshold analysis - same
deterministic/no-LLM/no-writes separation as insights.py and
recovery_insights.py.

Scoping note: tier is a live, current-state label on each Customer, not a
dated fact - "which tier was this customer in during the last 7 days"
isn't a meaningful question the way "how much revenue in the last 7 days"
is. So the range picker here applies to the two genuinely activity-based
sections (tier-wise performance, dormant accounts) but NOT to tier
distribution, near-miss customers, or risk-flag redemption - those are
always a snapshot of customers as they stand right now, regardless of
range. Documented here so it isn't a mystery why changing the range
doesn't move every number.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app import runtime_flags
from app.insights import (  # re-exported, same presets everywhere
    RANGE_PRESETS, resolve_range, LOW_SAMPLE_THRESHOLD, MAX_PATTERNS, _PATTERN_PRIORITY, _inr,
)
from app.models import Customer, CustomerTier, Order, OrderStatus, CartEvent, CartEventType
from app.tiering import tier_breakdown


def _all_tiers() -> list[str]:
    return [t.value for t in CustomerTier]


def tier_distribution(db: Session) -> dict:
    """Current snapshot - not range-scoped, see module docstring."""
    counts = {t: 0 for t in _all_tiers()}
    for c in db.query(Customer).all():
        counts[c.tier.value] = counts.get(c.tier.value, 0) + 1
    return counts


def _failed_payment_losses_by_tier(orders: list[Order], tier_by_customer: dict) -> dict[str, float]:
    """How much of each tier's money is genuinely LOST via the failed-
    payment thread - not "hasn't succeeded yet", which is what this used
    to mean back when it was derived from _resolved_streak (no later
    same-basket order ever captured). That definition predates
    Order.risk_settled: today the real ledger only calls a failed run
    lost once its carrier settles via give-up or the maintenance sweep's
    lapse - a customer who failed once today and simply hasn't retried
    yet is NOT lost, they're still open in at_risk_failed.

    Can't just sum amount_inr for every FAILED order with risk_settled
    True, either - that double(triple, ...)-counts a multi-retry run: a
    RETRY's risk_settled flips True immediately the moment it fails (see
    webhook.py::_open_run_carrier - only the run's first/carrier order
    stays False until the run concludes), so 3 failed tries at one
    purchase would wrongly look like 3 separate losses. Only the
    CARRIER - the earliest FAILED order for a given (customer, basket)
    run - could ever have been genuinely settled; group by that, take
    the earliest per group, count it once if and only if it settled.

    Scope note: grouping only sees FAILED orders already inside the
    selected range (the same `orders` tier_wise_performance already
    fetched) - a run whose carrier predates the range but whose retry
    falls inside it would be (mis)read as its own one-order group. Rare
    in practice (retries cluster close in time) and, even when it
    happens, is a smaller version of the SAME direction of error the old
    heuristic made on every run, not a new failure mode - not worth a
    second, unscoped query to close entirely.
    """
    groups: dict[tuple, list[Order]] = {}
    for o in orders:
        if o.status != OrderStatus.FAILED:
            continue
        try:
            items = json.loads(o.items_json)
        except (TypeError, ValueError):
            continue
        key = (o.customer_id, tuple(sorted((i.get("sku"), i.get("qty")) for i in items)))
        groups.setdefault(key, []).append(o)

    losses_by_tier: dict[str, float] = {}
    for (customer_id, _sig), group in groups.items():
        carrier = min(group, key=lambda o: o.created_at)
        if carrier.risk_settled:
            tier = tier_by_customer.get(customer_id, "unknown")
            losses_by_tier[tier] = losses_by_tier.get(tier, 0.0) + carrier.amount_inr
    return losses_by_tier


def tier_wise_performance(db: Session, since: datetime | None) -> list[dict]:
    """Per tier: successes, failures, revenue captured, revenue genuinely
    lost (see _failed_payment_losses_by_tier - a run settled to lost via
    give-up or the sweep's lapse, counted once per run, never "just
    hasn't succeeded yet"), incentives offered + their cost (reuses
    Incentive Analysis's own per-bucket numbers rather than re-deriving),
    net gain (now actually net - captured minus discount cost minus
    losses, not blind to the loss side), cancellations, and average
    order value."""
    from app.insights import bucket_metrics  # local import - avoids a module-level cycle with insights.py

    q = db.query(Order).filter(Order.status.in_([OrderStatus.CAPTURED, OrderStatus.FAILED]))
    if since:
        q = q.filter(Order.created_at >= since)
    orders = q.all()

    customer_ids = list({o.customer_id for o in orders})
    tier_by_customer = {
        c.id: c.tier.value for c in db.query(Customer).filter(Customer.id.in_(customer_ids)).all()
    } if customer_ids else {}

    incentive_buckets = bucket_metrics(db, since)
    incentive_by_tier: dict[str, dict] = {}
    for b in incentive_buckets:
        agg = incentive_by_tier.setdefault(b["tier"], {"offered": 0, "discount_given_inr": 0.0})
        agg["offered"] += b["incentive_offered"]
        agg["discount_given_inr"] += b["discount_given_inr"]

    cancel_q = db.query(CartEvent).filter(CartEvent.event_type == CartEventType.EXPLICIT_CANCEL)
    if since:
        cancel_q = cancel_q.filter(CartEvent.created_at >= since)
    cancels_by_tier: dict[str, int] = {}
    for ce in cancel_q.all():
        cancels_by_tier[ce.tier_at_time.value] = cancels_by_tier.get(ce.tier_at_time.value, 0) + 1

    losses_by_tier = _failed_payment_losses_by_tier(orders, tier_by_customer)

    per_tier: dict[str, dict] = {t: {"orders": [], "captured": []} for t in _all_tiers()}
    for o in orders:
        tier = tier_by_customer.get(o.customer_id, "unknown")
        per_tier.setdefault(tier, {"orders": [], "captured": []})
        per_tier[tier]["orders"].append(o)
        if o.status == OrderStatus.CAPTURED:
            per_tier[tier]["captured"].append(o)

    result = []
    for tier in _all_tiers():
        data = per_tier.get(tier, {"orders": [], "captured": []})
        total_orders = len(data["orders"])
        successful = len(data["captured"])
        failed = total_orders - successful
        revenue_captured = round(sum(o.amount_inr for o in data["captured"]), 2)
        revenue_lost = round(losses_by_tier.get(tier, 0.0), 2)
        inc = incentive_by_tier.get(tier, {"offered": 0, "discount_given_inr": 0.0})
        discount_given = round(inc["discount_given_inr"], 2)
        net_gain = round(revenue_captured - discount_given - revenue_lost, 2)
        avg_order_value = round(revenue_captured / successful, 2) if successful else None

        result.append({
            "tier": tier,
            "successful_orders": successful,
            "failed_orders": failed,
            "revenue_captured_inr": revenue_captured,
            "revenue_lost_inr": revenue_lost,
            "incentives_offered": inc["offered"],
            "discount_given_inr": discount_given,
            "net_gain_inr": net_gain,
            "cancellations": cancels_by_tier.get(tier, 0),
            "avg_order_value_inr": avg_order_value,
        })
    return result


def near_miss_customers(db: Session) -> dict:
    """Not range-scoped - see module docstring. Customers sitting right at
    a tier boundary, by name/email since knowing WHO matters here.

    Much simpler than it used to be, because there is now one number
    behind every boundary. The old version needed three hand-written
    queries against specific enum values plus a separate cancel gate;
    "within N points of the next threshold" works for every boundary at
    once and can't fall out of sync with compute_tier(), since both read
    the same tier_breakdown().

    Three lists:
    - close_to_promotion: on score, but short on purchase volume
    - close_on_score: have the volume, just short on score
    - close_to_risk: one bad behaviour away from tripping the risk gate
    """
    near_score_points = 10

    close_to_promotion = []
    close_on_score = []
    close_to_risk = []

    risk_min_attempts = runtime_flags.get_tier_risk_min_attempts()
    max_fail_rate = runtime_flags.get_tier_risk_attributable_failure_rate()
    max_cancel_rate = runtime_flags.get_tier_risk_cancel_rate()

    for c in db.query(Customer).all():
        result = tier_breakdown(db, c.id)
        base = {"id": c.id, "email": c.email, "name": c.name,
                "tier": result["tier"].value, "engagement_score": result["score"]}
        nxt = result["next_tier"]

        if nxt:
            # Volume is the only thing holding them back - a merchant can
            # act on this differently (nudge them to buy again) than on
            # someone whose score itself is short.
            if nxt["score_gap"] == 0 and nxt["attempts_gap"] > 0:
                close_to_promotion.append({
                    **base, "next_tier": nxt["tier"],
                    "purchases_needed": nxt["attempts_gap"],
                })
            elif 0 < nxt["score_gap"] <= near_score_points and nxt["attempts_gap"] == 0:
                close_on_score.append({
                    **base, "next_tier": nxt["tier"], "points_needed": nxt["score_gap"],
                })

        if result["tier"] == CustomerTier.RISK:
            continue

        inputs = result["inputs"]
        # One more attributable failure / one more cancel - would either
        # gate trip? Mirrors _risk_gate's own arithmetic rather than
        # approximating it.
        attempts_after = inputs["purchase_attempts"] + 1
        fails_after = inputs["attributable_failed_attempts"] + 1
        would_fail_gate = (
            attempts_after >= risk_min_attempts and (fails_after / attempts_after) > max_fail_rate
        )
        cancels_after = inputs["explicit_cancels"] + 1
        cancel_intents_after = cancels_after + inputs["successful_attempts"]
        would_cancel_gate = (
            cancel_intents_after >= risk_min_attempts
            and (cancels_after / cancel_intents_after) > max_cancel_rate
        )
        if would_fail_gate or would_cancel_gate:
            close_to_risk.append({
                **base,
                "trigger": "another failed payment" if would_fail_gate else "another cancellation",
            })

    return {
        "close_to_promotion": close_to_promotion,
        "close_on_score": close_on_score,
        "close_to_risk": close_to_risk,
    }


def risk_flag_redemption(db: Session) -> dict:
    """Not range-scoped - see module docstring. Of customers permanently
    stuck in RISK because of a risk_block flag (not the dynamic
    success-rate path), how many have paid successfully since? Directly
    answers "were we too harsh on a single flag"."""
    risk_customers = db.query(Customer).filter(Customer.tier == CustomerTier.RISK).all()
    flagged = []
    for c in risk_customers:
        first_risk_order = db.query(Order).filter(
            Order.customer_id == c.id, Order.failure_reason == "risk_block",
        ).order_by(Order.created_at.asc()).first()
        if not first_risk_order:
            continue  # this customer is RISK via the success-rate path, not a permanent flag
        later_success = db.query(Order).filter(
            Order.customer_id == c.id, Order.status == OrderStatus.CAPTURED,
            Order.created_at > first_risk_order.created_at,
        ).count()
        flagged.append({
            "id": c.id, "email": c.email, "name": c.name,
            "flagged_at": first_risk_order.created_at.isoformat(),
            "successful_orders_since": later_success,
        })

    redeemed = [f for f in flagged if f["successful_orders_since"] > 0]
    return {
        "total_flagged_permanently": len(flagged),
        "redeemed_since_count": len(redeemed),
        "redeemed_customers": redeemed,
    }


def dormant_accounts_by_tier(db: Session, since: datetime | None) -> dict:
    """% of each tier with zero orders in the selected range (option B -
    activity-based, not signup-based). NEW tier will trivially show ~100%
    dormant by definition (no orders ever) - included for completeness,
    not because it's informative on its own."""
    result = {}
    for tier in _all_tiers():
        customers = db.query(Customer).filter(Customer.tier == tier).all()
        if not customers:
            result[tier] = {"total": 0, "dormant": 0, "dormant_pct": None}
            continue
        dormant = 0
        for c in customers:
            q = db.query(Order.id).filter(Order.customer_id == c.id)
            if since:
                q = q.filter(Order.created_at >= since)
            has_order = q.first() is not None
            if not has_order:
                dormant += 1
        result[tier] = {
            "total": len(customers), "dormant": dormant,
            "dormant_pct": round(100 * dormant / len(customers), 1),
        }
    return result


def config_snapshot() -> dict:
    return {
        "tier_loyal_score": runtime_flags.get_tier_loyal_score(),
        "tier_regular_score": runtime_flags.get_tier_regular_score(),
        "tier_min_attempts_for_loyal": runtime_flags.get_tier_min_attempts_for_loyal(),
        "tier_min_attempts_for_regular": runtime_flags.get_tier_min_attempts_for_regular(),
        "tier_target_orders_per_month": runtime_flags.get_tier_target_orders_per_month(),
        "tier_target_aov_inr": runtime_flags.get_tier_target_aov_inr(),
        "tier_recency_window_days": runtime_flags.get_tier_recency_window_days(),
        "tier_behavior_window_days": runtime_flags.get_tier_behavior_window_days(),
        "tier_risk_min_attempts": runtime_flags.get_tier_risk_min_attempts(),
        "tier_risk_attributable_failure_rate": runtime_flags.get_tier_risk_attributable_failure_rate(),
        "tier_risk_cancel_rate": runtime_flags.get_tier_risk_cancel_rate(),
    }


def score_distribution(db: Session) -> dict:
    """How the customer base is spread across the 0-100 engagement score.
    New section: with tiers now derived from one number, seeing where that
    number clusters is what tells a merchant whether a threshold sits in a
    sensible place or straight through the middle of a crowd."""
    buckets = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    scores = []
    for c in db.query(Customer).all():
        score = tier_breakdown(db, c.id)["score"]
        scores.append(score)
        if score < 20:
            buckets["0-19"] += 1
        elif score < 40:
            buckets["20-39"] += 1
        elif score < 60:
            buckets["40-59"] += 1
        elif score < 80:
            buckets["60-79"] += 1
        else:
            buckets["80-100"] += 1
    return {
        "buckets": buckets,
        "median_score": sorted(scores)[len(scores) // 2] if scores else None,
        "customers_scored": len(scores),
    }


_ELIGIBLE_LADDER_TIERS = ("casual", "regular", "loyal")


def generate_tier_patterns(
    tier_distribution: dict, tier_wise_performance: list[dict], near_miss_customers: dict,
    risk_flag_redemption: dict, dormant_accounts_by_tier: dict,
) -> list[dict]:
    """Turns the tables/lists above into plain-English observations a
    merchant can act on - same synthesis layer, same {"kind", "text"}
    shape and priority ordering as insights.py::generate_patterns. Pure
    derivation from data already computed above: no new queries, no LLM
    call, works even when Groq is unreachable.

    Not every pattern here maps to a whitelisted param the LLM's
    recommendation flow can apply - some (near-miss customers, a
    long-held risk flag) are meant for the merchant to act on directly,
    which is a legitimate form of insight distinct from an auto-suggested
    config change.
    """
    patterns: list[dict] = []
    perf_by_tier = {row["tier"]: row for row in tier_wise_performance}

    # 1. Strongest tier - highest (now genuinely net) gain, among tiers
    # with real order volume.
    active = [row for row in tier_wise_performance
              if row["tier"] in _ELIGIBLE_LADDER_TIERS and row["successful_orders"] > 0]
    if active:
        best = max(active, key=lambda row: row["net_gain_inr"])
        patterns.append({
            "kind": "positive",
            "text": f"{best['tier'].capitalize()} is your strongest segment this period - "
                    f"₹{_inr(best['net_gain_inr'])} net from {best['successful_orders']} successful orders.",
        })

    # 2. Promotion-ready customers.
    promo = near_miss_customers.get("close_to_promotion", [])
    score = near_miss_customers.get("close_on_score", [])
    if promo or score:
        total = len(promo) + len(score)
        parts = []
        if promo:
            parts.append(f"{len(promo)} just need one more purchase")
        if score:
            parts.append(f"{len(score)} just need a few more engagement points")
        patterns.append({
            "kind": "opportunity",
            "text": f"{total} customer{'s' if total != 1 else ''} "
                    f"{'are' if total != 1 else 'is'} right at the edge of moving up "
                    f"a tier ({', '.join(parts)}) - worth a nudge before they drift.",
        })

    # 3. Risk flag possibly held too long - only from a real sample, and
    # only when a meaningful share have actually redeemed themselves.
    total_flagged = risk_flag_redemption.get("total_flagged_permanently", 0)
    redeemed = risk_flag_redemption.get("redeemed_since_count", 0)
    if total_flagged >= 3 and (redeemed / total_flagged) >= 0.3:
        patterns.append({
            "kind": "opportunity",
            "text": f"{redeemed} of {total_flagged} permanently risk-flagged customers have paid "
                    f"successfully since being flagged - worth checking whether the flag is being "
                    f"held onto longer than it should.",
        })

    # 4. Close to tripping the Risk gate.
    close_to_risk = near_miss_customers.get("close_to_risk", [])
    if len(close_to_risk) >= 2:
        patterns.append({
            "kind": "warning",
            "text": f"{len(close_to_risk)} customers are one more bad event away from being "
                    f"flagged Risk.",
        })

    # 5. Dormant high-value tier.
    for tier in ("loyal", "regular"):
        d = dormant_accounts_by_tier.get(tier)
        if d and d.get("total", 0) >= 5 and (d.get("dormant_pct") or 0) > 40:
            patterns.append({
                "kind": "warning",
                "text": f"{d['dormant_pct']}% of your {tier.capitalize()} customers haven't ordered "
                        f"in this window ({d['dormant']}/{d['total']}) - worth a win-back nudge "
                        f"before they slide down a tier.",
            })

    # 6. Risk tier scale, as context.
    total_customers = sum(tier_distribution.values())
    risk_count = tier_distribution.get("risk", 0)
    if total_customers >= 10 and risk_count / total_customers > 0.1:
        patterns.append({
            "kind": "info",
            "text": f"{risk_count} customers ({round(100 * risk_count / total_customers)}%) are "
                    f"currently tagged Risk - worth checking whether the failure/cancel-rate "
                    f"thresholds are catching the right slice.",
        })

    # 7. Low activity, lowest priority - context, not a finding.
    total_orders = sum(row["successful_orders"] + row["failed_orders"] for row in tier_wise_performance)
    if 0 < total_orders < LOW_SAMPLE_THRESHOLD:
        patterns.append({
            "kind": "info",
            "text": "Not much order activity in this window yet - these numbers may not be "
                     "reliable until more data comes in.",
        })

    patterns.sort(key=lambda p: _PATTERN_PRIORITY[p["kind"]])
    return patterns[:MAX_PATTERNS]


def build_analysis_input(db: Session, range_key: str) -> dict:
    since, normalized_range = resolve_range(range_key)
    perf = tier_wise_performance(db, since)
    nm = near_miss_customers(db)
    rr = risk_flag_redemption(db)
    dormant = dormant_accounts_by_tier(db, since)
    dist = tier_distribution(db)
    return {
        "range": normalized_range,
        "since": since.isoformat() if since else None,
        "generated_at": datetime.utcnow().isoformat(),
        "config_snapshot": config_snapshot(),
        "tier_distribution": dist,
        "tier_wise_performance": perf,
        "score_distribution": score_distribution(db),
        "near_miss_customers": nm,
        "risk_flag_redemption": rr,
        "dormant_accounts_by_tier": dormant,
        "patterns": generate_tier_patterns(dist, perf, nm, rr, dormant),
    }

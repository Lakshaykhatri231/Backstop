"""
Pure aggregation layer for merchant-facing incentive / cart-economics
analysis. No LLM calls here and no writes - see insights_llm.py for the
layer that turns these numbers into recommendations, and
insights_router.py for the endpoints that wire the two together and audit
the result. Kept separate on purpose (same reason rules_engine.py is kept
separate from llm_agent.py): these numbers should be inspectable and
testable on their own, independent of whether the LLM call is even
working.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import runtime_flags
from app.models import CartEvent, CartEventType, CartEventStatus, AgentAction, Order, OrderStatus

# Preset ranges only (no free-form date picker) - matches the storefront's
# existing button-driven style, and covers the actual use case (compare
# before/after a config change) better than an arbitrary range would.
RANGE_PRESETS = {"7d": 7, "30d": 30, "all": None}

# Below this many events in a bucket, flag the bucket rather than let a
# tiny sample read as a confident trend.
LOW_SAMPLE_THRESHOLD = 10

# How many sampled reasoning strings to hand the LLM per bucket - enough
# for qualitative grounding, not the whole corpus.
REASONING_SAMPLES_PER_BUCKET = 2


def resolve_range(range_key: str) -> tuple[datetime | None, str]:
    """Returns (since_datetime_or_None, normalized_range_key)."""
    if range_key not in RANGE_PRESETS:
        range_key = "all"
    days = RANGE_PRESETS[range_key]
    since = (datetime.utcnow() - timedelta(days=days)) if days else None
    return since, range_key


def _converted(db: Session, cart_event_id: str) -> bool:
    """Did this cart event turn into an actual paid order? Works the same
    way regardless of action type: offer_incentive and send_resume_link
    both set CartEvent.status, but a plain silent-abandon reminder never
    does (nothing for the customer to click) - it's tracked instead via
    the invisible PendingSignal(timeout_attribution) marker, which
    /checkout also resolves into Order.recovered_from_cart_event_id. So
    checking that one field covers every action type without needing to
    branch on which kind of event this was."""
    return db.query(Order.id).filter(
        Order.recovered_from_cart_event_id == cart_event_id,
        Order.status == OrderStatus.CAPTURED,
    ).first() is not None


def config_snapshot() -> dict:
    """The whitelist of parameters this whole analysis is allowed to touch.
    Anything not in this dict, the LLM layer must never suggest changing.
    Reads through runtime_flags, NOT settings directly - settings is the
    static .env-loaded value, and would show stale numbers here the moment
    a merchant applies a suggestion (runtime_flags is what apply-suggestion
    actually mutates - see app/insights_router.py)."""
    pct_bands = runtime_flags.get_incentive_pct_bands()
    freq_caps = runtime_flags.get_incentive_freq_caps()
    amount_caps = runtime_flags.get_incentive_amount_caps()
    eligible_tiers = runtime_flags.get_incentive_eligible_tiers()

    return {
        # Per-tier now, not one flat rate. Each tier has a discount BAND;
        # a customer's exact % inside it comes from their engagement score.
        "incentive_pct_bands": pct_bands,
        "incentive_max_per_30d_by_tier": freq_caps,
        "incentive_amount_caps_by_tier": amount_caps,
        "nudge_expiry_hours": runtime_flags.get_nudge_expiry_hours(),
        "incentive_eligible_tiers": sorted(eligible_tiers),
        # Flat view, keyed by the EXACT param names in insights_llm.VALID_PARAMS
        # (kept in sync by hand - same convention as adding a new tunable param
        # already requires touching insights_llm.VALID_PARAMS + insights_router's
        # _NUMERIC_PARAMS/_ORDERED_PAIRS together). The groupings above are for
        # human/UI readability, but a recommendation addresses a param by its
        # flat name - asking the LLM to reverse-engineer "incentive_max_order_
        # value_casual means incentive_amount_caps_by_tier.casual" was a real
        # bug, not a wording nicety: it was producing "unknown" current_value
        # in recommendations whenever it couldn't confidently make that jump.
        # This is the single, unambiguous source for "what is the CURRENT
        # value of every whitelisted param" - one direct key lookup, no
        # inference required.
        "current_values": {
            "incentive_max_order_value_casual": amount_caps.get("casual"),
            "incentive_max_order_value_regular": amount_caps.get("regular"),
            "incentive_max_order_value_loyal": amount_caps.get("loyal"),
            "nudge_expiry_hours": runtime_flags.get_nudge_expiry_hours(),
            "casual_tier_incentive_eligible": "casual" in eligible_tiers,
            "incentive_pct_casual_min": (pct_bands.get("casual") or [None, None])[0],
            "incentive_pct_casual_max": (pct_bands.get("casual") or [None, None])[1],
            "incentive_pct_regular_min": (pct_bands.get("regular") or [None, None])[0],
            "incentive_pct_regular_max": (pct_bands.get("regular") or [None, None])[1],
            "incentive_pct_loyal_min": (pct_bands.get("loyal") or [None, None])[0],
            "incentive_pct_loyal_max": (pct_bands.get("loyal") or [None, None])[1],
            "incentive_max_per_30d_casual": freq_caps.get("casual"),
            "incentive_max_per_30d_regular": freq_caps.get("regular"),
            "incentive_max_per_30d_loyal": freq_caps.get("loyal"),
        },
    }


def bucket_metrics(db: Session, since: datetime | None) -> list[dict]:
    """One row per (tier_at_time, event_type) bucket that has any events in
    the window. Every count/rate a merchant would need to answer "is this
    incentive profitable, should its scope change, is the cap doing
    anything" - see the conversation this was scoped from."""
    q = db.query(CartEvent)
    if since:
        q = q.filter(CartEvent.created_at >= since)
    events = q.all()

    groups: dict[tuple[str, str], list[CartEvent]] = {}
    for e in events:
        groups.setdefault((e.tier_at_time.value, e.event_type.value), []).append(e)

    result = []
    for (tier, event_type), group in sorted(groups.items()):
        incentive_events = [e for e in group if e.action == AgentAction.OFFER_INCENTIVE]
        non_incentive_events = [
            e for e in group
            if e.action in (AgentAction.SEND_REMINDER, AgentAction.SEND_RESUME_LINK)
        ]

        incentive_converted = [e for e in incentive_events if _converted(db, e.id)]
        non_incentive_converted = [e for e in non_incentive_events if _converted(db, e.id)]

        # Only count discount for offers actually REDEEMED (status RESUMED
        # with real terms snapshotted) - mirrors revenue.py's own
        # _book_incentive_cost_if_redeemed exactly, so this figure can
        # never disagree with the real incentive_cost ledger line. Must be
        # derived from the charge actually made and the stored %, NOT
        # offer-time amount minus final amount: since discounts now float
        # with an edited cart, the offer-time amount can describe a
        # completely different basket than what was actually charged.
        discount_given_inr = 0.0
        for e in incentive_events:
            if (e.status == CartEventStatus.RESUMED
                    and e.incentive_final_amount_inr is not None and e.incentive_pct):
                pct = e.incentive_pct
                if pct < 100:
                    discount_given_inr += e.incentive_final_amount_inr * pct / (100.0 - pct)
        discount_given_inr = round(discount_given_inr, 2)

        avg_incentive_pct_given = (
            round(sum(e.incentive_pct for e in incentive_events if e.incentive_pct) / len(incentive_events), 1)
            if incentive_events else None
        )

        revenue_recovered_inr = 0.0
        for e in incentive_converted:
            captured = db.query(Order.amount_inr).filter(
                Order.recovered_from_cart_event_id == e.id,
                Order.status == OrderStatus.CAPTURED,
            ).scalar()
            revenue_recovered_inr += captured or 0.0
        revenue_recovered_inr = round(revenue_recovered_inr, 2)

        net_recovered_inr = round(revenue_recovered_inr - discount_given_inr, 2)

        # Cap-hit breakdown - only meaningful for events where the tier
        # branch even considers an incentive at all (RISK/NEW never do, so
        # their amount_cap_ok/freq_cap_ok/tier_incentive_eligible are null -
        # excluded here rather than miscounted as "blocked").
        gated_events = [e for e in group if e.tier_incentive_eligible is not None]
        freq_cap_blocked = len([e for e in gated_events if e.freq_cap_ok is False])
        amount_cap_blocked = len([e for e in gated_events if e.amount_cap_ok is False])
        tier_gate_blocked = len([e for e in gated_events if e.tier_incentive_eligible is False])

        sample_size = len(group)
        incentive_redemption_rate_pct = _rate_pct(len(incentive_converted), len(incentive_events))
        reminder_conversion_rate_pct = _rate_pct(len(non_incentive_converted), len(non_incentive_events))

        result.append({
            "tier": tier,
            "event_type": event_type,
            "sample_size": sample_size,
            "low_sample": sample_size < LOW_SAMPLE_THRESHOLD,
            "incentive_offered": len(incentive_events),
            "incentive_redeemed": len(incentive_converted),
            "incentive_redemption_rate_pct": incentive_redemption_rate_pct,
            "avg_incentive_pct_given": avg_incentive_pct_given,
            "reminder_or_resume_link_offered": len(non_incentive_events),
            "reminder_or_resume_link_converted": len(non_incentive_converted),
            "reminder_or_resume_link_conversion_rate_pct": reminder_conversion_rate_pct,
            # How much better (or worse) the incentive did than a plain,
            # no-discount nudge for the SAME tier/trigger - the direct
            # "did the money actually buy anything" number. Null when
            # either side has no denominator to compare.
            "incentive_lift_pct": (
                round(incentive_redemption_rate_pct - reminder_conversion_rate_pct, 1)
                if incentive_redemption_rate_pct is not None and reminder_conversion_rate_pct is not None
                else None
            ),
            "discount_given_inr": discount_given_inr,
            "revenue_recovered_inr": revenue_recovered_inr,
            "net_recovered_inr": net_recovered_inr,
            "freq_cap_blocked_count": freq_cap_blocked,
            "amount_cap_blocked_count": amount_cap_blocked,
            "tier_gate_blocked_count": tier_gate_blocked,
        })
    return result


def overview_metrics(buckets: list[dict], config_snapshot: dict) -> dict:
    """Rolls the per-(tier, event_type) buckets up to one row per TIER
    (merging silent_abandon + explicit_cancel) plus one overall total -
    the merchant-facing synthesis a raw bucket table can't answer on its
    own: did incentives work, how much, which tier actually recovers best.

    Aggregates the underlying COUNTS/AMOUNTS first and re-derives every
    rate from those sums - never averages the buckets' own pre-computed
    rates, which would be wrong the moment bucket sizes differ.
    """
    pct_bands = config_snapshot.get("incentive_pct_bands", {})

    tiers: dict[str, dict] = {}
    for b in buckets:
        t = tiers.setdefault(b["tier"], {
            "sample_size": 0, "incentive_offered": 0, "incentive_redeemed": 0,
            "reminder_or_resume_link_offered": 0, "reminder_or_resume_link_converted": 0,
            "discount_given_inr": 0.0, "revenue_recovered_inr": 0.0, "net_recovered_inr": 0.0,
            "incentive_pct_weighted_sum": 0.0,
            "freq_cap_blocked": 0, "amount_cap_blocked": 0, "tier_gate_blocked": 0,
        })
        t["sample_size"] += b["sample_size"]
        t["incentive_offered"] += b["incentive_offered"]
        t["incentive_redeemed"] += b["incentive_redeemed"]
        t["reminder_or_resume_link_offered"] += b["reminder_or_resume_link_offered"]
        t["reminder_or_resume_link_converted"] += b["reminder_or_resume_link_converted"]
        t["discount_given_inr"] += b["discount_given_inr"]
        t["revenue_recovered_inr"] += b["revenue_recovered_inr"]
        t["net_recovered_inr"] += b["net_recovered_inr"]
        if b["avg_incentive_pct_given"] is not None:
            t["incentive_pct_weighted_sum"] += b["avg_incentive_pct_given"] * b["incentive_offered"]
        t["freq_cap_blocked"] += b["freq_cap_blocked_count"]
        t["amount_cap_blocked"] += b["amount_cap_blocked_count"]
        t["tier_gate_blocked"] += b["tier_gate_blocked_count"]

    totals = {
        "incentive_offered": 0, "incentive_redeemed": 0,
        "reminder_or_resume_link_offered": 0, "reminder_or_resume_link_converted": 0,
        "discount_given_inr": 0.0, "revenue_recovered_inr": 0.0, "net_recovered_inr": 0.0,
    }
    leaderboard = []
    for tier, t in tiers.items():
        if t["incentive_offered"] == 0 and t["reminder_or_resume_link_offered"] == 0:
            continue  # no incentive-relevant activity at all this range (e.g. new/risk)

        for key in totals:
            totals[key] += t[key]

        redemption_rate_pct = _rate_pct(t["incentive_redeemed"], t["incentive_offered"])
        baseline_rate_pct = _rate_pct(t["reminder_or_resume_link_converted"], t["reminder_or_resume_link_offered"])
        avg_pct_given = (
            round(t["incentive_pct_weighted_sum"] / t["incentive_offered"], 1)
            if t["incentive_offered"] else None
        )
        leaderboard.append({
            "tier": tier,
            "sample_size": t["sample_size"],
            "low_sample": t["sample_size"] < LOW_SAMPLE_THRESHOLD,
            "incentive_offered": t["incentive_offered"],
            "incentive_redeemed": t["incentive_redeemed"],
            "redemption_rate_pct": redemption_rate_pct,
            "baseline_conversion_rate_pct": baseline_rate_pct,
            "lift_pct": (
                round(redemption_rate_pct - baseline_rate_pct, 1)
                if redemption_rate_pct is not None and baseline_rate_pct is not None else None
            ),
            "avg_incentive_pct_given": avg_pct_given,
            "incentive_pct_band": pct_bands.get(tier),
            "discount_given_inr": round(t["discount_given_inr"], 2),
            "revenue_recovered_inr": round(t["revenue_recovered_inr"], 2),
            "net_recovered_inr": round(t["net_recovered_inr"], 2),
        })

    leaderboard.sort(key=lambda row: row["net_recovered_inr"], reverse=True)

    # "Best"/"worst" restricted to tiers with enough sample to trust -
    # crowning a 2-event tier "best" because it happened to convert once
    # would be a confident-sounding lie.
    qualifying = [row for row in leaderboard if not row["low_sample"]]
    best_tier = qualifying[0]["tier"] if qualifying else None
    worst_tier = qualifying[-1]["tier"] if len(qualifying) >= 2 else None

    redemption_rate_pct = _rate_pct(totals["incentive_redeemed"], totals["incentive_offered"])
    baseline_rate_pct = _rate_pct(totals["reminder_or_resume_link_converted"], totals["reminder_or_resume_link_offered"])

    blocking_summary = []
    for tier, t in tiers.items():
        total_blocked = t["freq_cap_blocked"] + t["amount_cap_blocked"] + t["tier_gate_blocked"]
        if total_blocked > 0:
            blocking_summary.append({
                "tier": tier,
                "freq_cap_blocked": t["freq_cap_blocked"],
                "amount_cap_blocked": t["amount_cap_blocked"],
                "tier_gate_blocked": t["tier_gate_blocked"],
                "total_blocked": total_blocked,
            })
    blocking_summary.sort(key=lambda row: row["total_blocked"], reverse=True)

    return {
        "totals": {
            "incentive_offered": totals["incentive_offered"],
            "incentive_redeemed": totals["incentive_redeemed"],
            "redemption_rate_pct": redemption_rate_pct,
            "reminder_or_resume_link_offered": totals["reminder_or_resume_link_offered"],
            "reminder_or_resume_link_converted": totals["reminder_or_resume_link_converted"],
            "baseline_conversion_rate_pct": baseline_rate_pct,
            "lift_pct": (
                round(redemption_rate_pct - baseline_rate_pct, 1)
                if redemption_rate_pct is not None and baseline_rate_pct is not None else None
            ),
            "discount_given_inr": round(totals["discount_given_inr"], 2),
            "revenue_recovered_inr": round(totals["revenue_recovered_inr"], 2),
            "net_recovered_inr": round(totals["net_recovered_inr"], 2),
        },
        "tier_leaderboard": leaderboard,
        "best_tier": best_tier,
        "worst_tier": worst_tier,
        "blocking_summary": blocking_summary,
    }


def _rate_pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(100 * numerator / denominator, 1)


def _inr(x: float) -> str:
    return f"{x:,.0f}"


# Cap on how many patterns get shown - the whole point is a merchant can
# scan it in one pass, not read a second wall of text instead of a wall
# of numbers.
MAX_PATTERNS = 6

# opportunity > warning > positive > info: an actionable "you could
# change this" observation earns the top of the list over a plain FYI.
_PATTERN_PRIORITY = {"opportunity": 0, "warning": 1, "positive": 2, "info": 3}


def generate_patterns(overview: dict, config_snapshot: dict) -> list[dict]:
    """Turns overview_metrics()'s numbers into plain-English observations a
    merchant can act on without translating jargon themselves first -
    "redemption rate" and "freq-cap blocked" are correct internal names
    for internal gates, not how anyone describes their own program. Pure
    synthesis of numbers already computed above: no new metrics, no DB
    queries, no LLM call - this must work even when Groq is unreachable,
    same reason bucket_metrics/overview_metrics never touch the network.

    Each item: {"kind": "opportunity"|"warning"|"positive"|"info", "text": str}.
    "kind" drives icon/color on the frontend; ordering below is priority,
    re-sorted and capped at MAX_PATTERNS before returning.
    """
    totals = overview["totals"]
    board = overview["tier_leaderboard"]
    board_by_tier = {row["tier"]: row for row in board}
    blocking_by_tier = {row["tier"]: row for row in overview["blocking_summary"]}
    eligible_tiers = set(config_snapshot.get("incentive_eligible_tiers", []))

    patterns: list[dict] = []

    # 1. Overall verdict - the one thing a merchant wants to know first.
    if totals["incentive_offered"] == 0:
        patterns.append({
            "kind": "info",
            "text": "No discounts have been offered yet in this time range - come back "
                     "once some abandoned or cancelled carts have been seen.",
        })
    else:
        discount, recovered, net = totals["discount_given_inr"], totals["revenue_recovered_inr"], totals["net_recovered_inr"]
        if net > 0:
            patterns.append({
                "kind": "positive",
                "text": f"This incentive program is paying for itself: ₹{_inr(discount)} given away in "
                        f"discounts recovered ₹{_inr(recovered)} - a net gain of ₹{_inr(net)}.",
            })
        else:
            patterns.append({
                "kind": "warning",
                "text": f"This incentive program is currently costing more than it's recovering: "
                        f"₹{_inr(discount)} given away against only ₹{_inr(recovered)} recovered.",
            })

        # 2. Discount vs. doing nothing, in plain comparison terms.
        if totals["lift_pct"] is not None:
            if totals["lift_pct"] > 5:
                patterns.append({
                    "kind": "positive",
                    "text": f"Customers offered a discount came back {totals['redemption_rate_pct']}% of "
                            f"the time, versus {totals['baseline_conversion_rate_pct']}% for those who "
                            f"only got a plain reminder - the discount is clearly moving the needle.",
                })
            elif totals["lift_pct"] <= 0:
                patterns.append({
                    "kind": "warning",
                    "text": f"A plain reminder converted about as well as a discount this period "
                            f"({totals['baseline_conversion_rate_pct']}% vs {totals['redemption_rate_pct']}%) "
                            f"- you may be giving away money that wasn't needed to bring these customers back.",
                })

    # 3. Best tier.
    if overview["best_tier"]:
        row = board_by_tier[overview["best_tier"]]
        patterns.append({
            "kind": "positive",
            "text": f"{row['tier'].capitalize()} customers respond best to discounts - "
                    f"{row['redemption_rate_pct']}% take the offer, recovering ₹{_inr(row['net_recovered_inr'])} "
                    f"net. Worth prioritizing your incentive budget here.",
        })

    # 4. Cap-constrained high performer - the cross-reference a flat
    # blocking list never made: a tier converting well is ALSO the one
    # being throttled by its own frequency cap.
    for row in board:
        b = blocking_by_tier.get(row["tier"])
        if (b and b["freq_cap_blocked"] > 0 and not row["low_sample"]
                and row["redemption_rate_pct"] is not None and row["redemption_rate_pct"] >= 50):
            patterns.append({
                "kind": "opportunity",
                "text": f"{row['tier'].capitalize()} customers are converting well with discounts "
                        f"({row['redemption_rate_pct']}%), but {b['freq_cap_blocked']} offer"
                        f"{'s were' if b['freq_cap_blocked'] != 1 else ' was'} skipped this period because "
                        f"they'd already used their monthly allowance. Raising this limit could recover more "
                        f"from your best-responding customers.",
            })

    # 5. Ineligible tier missing out - sets up exactly the whitelisted
    # param (standard_tier_incentive_eligible et al) the Implement button
    # already knows how to apply.
    for row in board:
        tier = row["tier"]
        if tier in ("new", "risk") or tier in eligible_tiers:
            continue
        if row["baseline_conversion_rate_pct"] is not None and row["reminder_or_resume_link_offered"] >= LOW_SAMPLE_THRESHOLD:
            patterns.append({
                "kind": "opportunity",
                "text": f"{tier.capitalize()} customers aren't currently eligible for any discount - "
                        f"{row['reminder_or_resume_link_offered']} of their carts got only a plain "
                        f"reminder this period, converting {row['baseline_conversion_rate_pct']}% of the "
                        f"time. If that's worth improving, consider opening incentives up to this tier.",
            })

    # 6. Weak tier - only worth a warning if genuinely low, not merely
    # last-place among otherwise-fine tiers.
    if overview["worst_tier"] and overview["worst_tier"] != overview["best_tier"]:
        row = board_by_tier[overview["worst_tier"]]
        if row["redemption_rate_pct"] is not None and row["redemption_rate_pct"] < 30:
            patterns.append({
                "kind": "warning",
                "text": f"{row['tier'].capitalize()} customers rarely take the discount when offered "
                        f"({row['redemption_rate_pct']}%) - worth checking whether the discount size or "
                        f"cart-value cap is the right fit for this tier.",
            })

    # 7. Low sample caution, lowest priority - context, not a finding.
    if 0 < totals["incentive_offered"] < LOW_SAMPLE_THRESHOLD:
        patterns.append({
            "kind": "info",
            "text": f"Only {totals['incentive_offered']} discount{'s' if totals['incentive_offered'] != 1 else ''} "
                    f"offered this period - not enough yet to draw firm conclusions.",
        })

    patterns.sort(key=lambda p: _PATTERN_PRIORITY[p["kind"]])
    return patterns[:MAX_PATTERNS]


def sample_reasoning(db: Session, since: datetime | None) -> list[dict]:
    """A handful of real reasoning strings per bucket, for qualitative
    grounding - not the whole corpus (that would bloat the prompt for no
    extra benefit)."""
    q = db.query(CartEvent)
    if since:
        q = q.filter(CartEvent.created_at >= since)
    events = q.order_by(CartEvent.created_at.desc()).all()

    seen_per_bucket: dict[tuple[str, str], int] = {}
    samples = []
    for e in events:
        key = (e.tier_at_time.value, e.event_type.value)
        if seen_per_bucket.get(key, 0) >= REASONING_SAMPLES_PER_BUCKET:
            continue
        seen_per_bucket[key] = seen_per_bucket.get(key, 0) + 1
        samples.append({
            "tier": e.tier_at_time.value,
            "event_type": e.event_type.value,
            "action": e.action.value if e.action else None,
            "reasoning": e.reasoning,
        })
    return samples


def build_analysis_input(db: Session, range_key: str) -> dict:
    """The full, self-contained input contract for the LLM layer - every
    number/setting it's allowed to see, nothing it isn't."""
    since, normalized_range = resolve_range(range_key)
    snapshot = config_snapshot()
    buckets = bucket_metrics(db, since)
    overview = overview_metrics(buckets, snapshot)
    return {
        "range": normalized_range,
        "since": since.isoformat() if since else None,
        "generated_at": datetime.utcnow().isoformat(),
        "config_snapshot": snapshot,
        "buckets": buckets,
        "overview": overview,
        "patterns": generate_patterns(overview, snapshot),
        "reasoning_samples": sample_reasoning(db, since),
    }

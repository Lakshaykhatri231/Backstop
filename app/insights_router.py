"""
Endpoints for the merchant-facing policy analyses:
1. Incentive/cart-economics analysis (aggregate -> LLM recommends -> audited -> apply)
2. Loss & recovery analysis across the whole funnel - abandoned carts,
   cancellations, failed payments and give-ups (same shape, different metrics)
3. Tier-threshold analysis (same shape again, plus a preview/commit step)
See app/insights.py + app/insights_llm.py for (1), app/recovery_insights.py +
app/recovery_insights_llm.py for (2), app/tier_insights.py + app/tier_insights_llm.py
for (3). All three share this one router and the same apply-suggestion endpoint,
since "validate param, cast, mutate runtime_flags, audit" is identical regardless
of which analysis a recommendation came from.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.audit import write_audit_entry
from app import runtime_flags
from app.insights import build_analysis_input, RANGE_PRESETS, config_snapshot as incentive_config_snapshot
from app.insights_llm import generate_recommendations, InsightsLLMError, VALID_PARAMS as INCENTIVE_PARAMS
from app.recovery_insights import build_analysis_input as build_recovery_analysis_input
from app.recovery_insights_llm import (
    generate_recommendations as generate_recovery_recommendations,
    RecoveryInsightsLLMError,
    VALID_PARAMS as RECOVERY_PARAMS,
)
from app.tier_insights import (
    build_analysis_input as build_tier_analysis_input,
    config_snapshot as tier_threshold_snapshot,
    tier_distribution,
)
from app.tier_insights_llm import (
    generate_recommendations as generate_tier_recommendations,
    TierInsightsLLMError,
    VALID_PARAMS as TIER_PARAMS,
    RATE_PARAMS as TIER_RATE_PARAMS,
    FLOAT_PARAMS as TIER_FLOAT_PARAMS,
)
from app.tiering import refresh_tier, compute_tier, ENGAGEMENT_WEIGHTS
from app.models import Customer

router = APIRouter()

# Params whose changes affect how every customer's tier is computed, not
# just future decisions - the router surfaces these differently (a
# warning, and the extra preview/commit re-evaluation step) rather than
# treating them like every other tunable param.
TIER_THRESHOLD_PARAMS = set(TIER_PARAMS)

# Every whitelisted param across both analyses, each mapped to (getter,
# setter) in runtime_flags, EXCEPT casual_tier_incentive_eligible
# (boolean tier toggle, handled separately) and confidence_threshold
# (float 0-1, handled separately) - everything else here is a plain
# non-negative integer.
_NUMERIC_PARAMS = {
    "incentive_max_order_value_casual": (
        runtime_flags.get_incentive_max_order_value_casual, runtime_flags.set_incentive_max_order_value_casual,
    ),
    "incentive_max_order_value_regular": (
        runtime_flags.get_incentive_max_order_value_regular, runtime_flags.set_incentive_max_order_value_regular,
    ),
    "incentive_max_order_value_loyal": (
        runtime_flags.get_incentive_max_order_value_loyal, runtime_flags.set_incentive_max_order_value_loyal,
    ),
    "nudge_expiry_hours": (
        runtime_flags.get_nudge_expiry_hours, runtime_flags.set_nudge_expiry_hours,
    ),
    # Per-tier discount bands and frequency caps. These replaced the single
    # flat incentive_discount_pct / incentive_max_per_customer_30d, so they
    # have to be reachable here or the Incentive Analysis has no economics
    # lever left to recommend at all.
    "incentive_pct_casual_min": (
        runtime_flags.get_incentive_pct_casual_min, runtime_flags.set_incentive_pct_casual_min,
    ),
    "incentive_pct_casual_max": (
        runtime_flags.get_incentive_pct_casual_max, runtime_flags.set_incentive_pct_casual_max,
    ),
    "incentive_pct_regular_min": (
        runtime_flags.get_incentive_pct_regular_min, runtime_flags.set_incentive_pct_regular_min,
    ),
    "incentive_pct_regular_max": (
        runtime_flags.get_incentive_pct_regular_max, runtime_flags.set_incentive_pct_regular_max,
    ),
    "incentive_pct_loyal_min": (
        runtime_flags.get_incentive_pct_loyal_min, runtime_flags.set_incentive_pct_loyal_min,
    ),
    "incentive_pct_loyal_max": (
        runtime_flags.get_incentive_pct_loyal_max, runtime_flags.set_incentive_pct_loyal_max,
    ),
    "incentive_max_per_30d_casual": (
        runtime_flags.get_incentive_max_per_30d_casual, runtime_flags.set_incentive_max_per_30d_casual,
    ),
    "incentive_max_per_30d_regular": (
        runtime_flags.get_incentive_max_per_30d_regular, runtime_flags.set_incentive_max_per_30d_regular,
    ),
    "incentive_max_per_30d_loyal": (
        runtime_flags.get_incentive_max_per_30d_loyal, runtime_flags.set_incentive_max_per_30d_loyal,
    ),
    "max_auto_retries": (
        runtime_flags.get_max_auto_retries, runtime_flags.set_max_auto_retries,
    ),
    "high_value_amount_inr": (
        runtime_flags.get_high_value_amount_inr, runtime_flags.set_high_value_amount_inr,
    ),
    "tier_loyal_score": (
        runtime_flags.get_tier_loyal_score, runtime_flags.set_tier_loyal_score,
    ),
    "tier_regular_score": (
        runtime_flags.get_tier_regular_score, runtime_flags.set_tier_regular_score,
    ),
    "tier_min_attempts_for_loyal": (
        runtime_flags.get_tier_min_attempts_for_loyal, runtime_flags.set_tier_min_attempts_for_loyal,
    ),
    "tier_min_attempts_for_regular": (
        runtime_flags.get_tier_min_attempts_for_regular, runtime_flags.set_tier_min_attempts_for_regular,
    ),
    "tier_target_aov_inr": (
        runtime_flags.get_tier_target_aov_inr, runtime_flags.set_tier_target_aov_inr,
    ),
    "tier_recency_window_days": (
        runtime_flags.get_tier_recency_window_days, runtime_flags.set_tier_recency_window_days,
    ),
    "tier_behavior_window_days": (
        runtime_flags.get_tier_behavior_window_days, runtime_flags.set_tier_behavior_window_days,
    ),
    "tier_risk_min_attempts": (
        runtime_flags.get_tier_risk_min_attempts, runtime_flags.set_tier_risk_min_attempts,
    ),
}

# Params that must keep an ordering relative to each other. Same idea as
# the old "risk rate must stay below trusted rate" check, generalised now
# that there are two ladder boundaries instead of one: a config where
# Regular's bar sits at or above Loyal's doesn't describe a ladder.
_ORDERED_PAIRS = [
    ("tier_regular_score", "tier_loyal_score",
     runtime_flags.get_tier_regular_score, runtime_flags.get_tier_loyal_score,
     "tier_regular_score must stay strictly below tier_loyal_score"),
    ("tier_min_attempts_for_regular", "tier_min_attempts_for_loyal",
     runtime_flags.get_tier_min_attempts_for_regular, runtime_flags.get_tier_min_attempts_for_loyal,
     "tier_min_attempts_for_regular must stay at or below tier_min_attempts_for_loyal"),
    # Discount bands: each tier's floor below its own ceiling, and no tier's
    # band reaching above the next tier's floor. Without this a merchant (or
    # an LLM suggestion) could hand CASUAL a bigger discount than LOYAL, which
    # inverts the whole reward ladder while every individual number still
    # looks reasonable on its own.
    ("incentive_pct_casual_min", "incentive_pct_casual_max",
     runtime_flags.get_incentive_pct_casual_min, runtime_flags.get_incentive_pct_casual_max,
     "incentive_pct_casual_min must stay at or below incentive_pct_casual_max"),
    ("incentive_pct_casual_max", "incentive_pct_regular_min",
     runtime_flags.get_incentive_pct_casual_max, runtime_flags.get_incentive_pct_regular_min,
     "casual's discount band must not reach above regular's floor"),
    ("incentive_pct_regular_min", "incentive_pct_regular_max",
     runtime_flags.get_incentive_pct_regular_min, runtime_flags.get_incentive_pct_regular_max,
     "incentive_pct_regular_min must stay at or below incentive_pct_regular_max"),
    ("incentive_pct_regular_max", "incentive_pct_loyal_min",
     runtime_flags.get_incentive_pct_regular_max, runtime_flags.get_incentive_pct_loyal_min,
     "regular's discount band must not reach above loyal's floor"),
    ("incentive_pct_loyal_min", "incentive_pct_loyal_max",
     runtime_flags.get_incentive_pct_loyal_min, runtime_flags.get_incentive_pct_loyal_max,
     "incentive_pct_loyal_min must stay at or below incentive_pct_loyal_max"),
    # Amount caps must scale up (or stay level) with tier - a config where
    # a lower tier can be incentivized on bigger carts than a higher one
    # inverts the trust ladder just like an inverted discount band would.
    ("incentive_max_order_value_casual", "incentive_max_order_value_regular",
     runtime_flags.get_incentive_max_order_value_casual, runtime_flags.get_incentive_max_order_value_regular,
     "casual's amount cap must stay at or below regular's"),
    ("incentive_max_order_value_regular", "incentive_max_order_value_loyal",
     runtime_flags.get_incentive_max_order_value_regular, runtime_flags.get_incentive_max_order_value_loyal,
     "regular's amount cap must stay at or below loyal's"),
]


def _check_ordering(param: str, parsed) -> None:
    """Reject a change that would inverse a ladder boundary, whichever of
    the pair is being changed."""
    for lower_name, upper_name, lower_get, upper_get, message in _ORDERED_PAIRS:
        if param not in (lower_name, upper_name):
            continue
        lower = parsed if param == lower_name else lower_get()
        upper = parsed if param == upper_name else upper_get()
        strict = lower_name == "tier_regular_score"
        bad = (lower >= upper) if strict else (lower > upper)
        if bad:
            raise HTTPException(
                400,
                f"{message} - got {lower_name}={lower}, {upper_name}={upper}. "
                "Rejected to avoid an inverted tier ladder.",
            )

_ALL_VALID_PARAMS = set(INCENTIVE_PARAMS) | set(RECOVERY_PARAMS) | set(TIER_PARAMS)


@router.get("/insights/incentive-analysis")
def incentive_analysis(range: str = "30d", db: Session = Depends(get_db)):
    """
    Aggregates cart-event incentive economics for the given window
    (7d / 30d / all) and asks the LLM for bounded, whitelisted
    recommendations.

    Metrics are always returned - they're a pure DB read, nothing to fail
    the way an LLM call can. Recommendations are omitted (with llm_error
    set) if the LLM call itself fails - never fabricated as a fallback,
    since there's no deterministic version of a policy recommendation to
    fall back to the way per-event decisions can fall back to the rules
    engine.
    """
    if range not in RANGE_PRESETS:
        raise HTTPException(400, f"range must be one of {list(RANGE_PRESETS)}")

    analysis_input = build_analysis_input(db, range)

    summary = ""
    recommendations: list[dict] = []
    llm_error = None
    try:
        result = generate_recommendations(analysis_input)
        summary = result["summary"]
        recommendations = result["recommendations"]
    except InsightsLLMError as e:
        llm_error = str(e)

    entry = write_audit_entry(
        db,
        action_type="policy_recommendation_generated",
        details={
            "range": analysis_input["range"],
            "buckets": analysis_input["buckets"],
            "overview": analysis_input["overview"],
            "patterns": analysis_input["patterns"],
            "config_snapshot": analysis_input["config_snapshot"],
            "summary": summary,
            "recommendations": recommendations,
            "llm_error": llm_error,
        },
    )

    return {
        "range": analysis_input["range"],
        "since": analysis_input["since"],
        "generated_at": analysis_input["generated_at"],
        "config_snapshot": analysis_input["config_snapshot"],
        "overview": analysis_input["overview"],
        "patterns": analysis_input["patterns"],
        "buckets": analysis_input["buckets"],
        "summary": summary,
        "recommendations": recommendations,
        "llm_error": llm_error,
        "audit_sequence_num": entry.sequence_num,
    }


@router.get("/insights/recovery-analysis")
def recovery_analysis(range: str = "30d", db: Session = Depends(get_db)):
    """
    Same shape as /insights/incentive-analysis, but for the whole loss
    funnel - abandoned carts, cancellations, failed payments and give-ups -
    tuning the ops params those pipelines share (confidence_threshold,
    max_auto_retries, high_value_amount_inr). See app/recovery_insights.py.

    The audit action_type stays "failure_policy_recommendation_generated"
    even though this endpoint was renamed from failure-analysis: renaming
    it would split the existing audit history across two names for no
    user-visible benefit.
    """
    if range not in RANGE_PRESETS:
        raise HTTPException(400, f"range must be one of {list(RANGE_PRESETS)}")

    analysis_input = build_recovery_analysis_input(db, range)

    summary = ""
    recommendations: list[dict] = []
    llm_error = None
    try:
        result = generate_recovery_recommendations(analysis_input)
        summary = result["summary"]
        recommendations = result["recommendations"]
    except RecoveryInsightsLLMError as e:
        llm_error = str(e)

    entry = write_audit_entry(
        db,
        action_type="failure_policy_recommendation_generated",
        details={
            "range": analysis_input["range"],
            "config_snapshot": analysis_input["config_snapshot"],
            "leak_summary": analysis_input["leak_summary"],
            "failure_reason_analysis": analysis_input["failure_reason_analysis"],
            "give_up_analysis": analysis_input["give_up_analysis"],
            "retry_effectiveness": analysis_input["retry_effectiveness"],
            "escalation_amount_analysis": analysis_input["escalation_amount_analysis"],
            "confidence_override_rates": analysis_input["confidence_override_rates"],
            "agent_reliability": analysis_input["agent_reliability"],
            "repeat_offenders_by_tier": analysis_input["repeat_offenders_by_tier"],
            "patterns": analysis_input["patterns"],
            "summary": summary,
            "recommendations": recommendations,
            "llm_error": llm_error,
        },
    )

    return {
        **analysis_input,
        "summary": summary,
        "recommendations": recommendations,
        "llm_error": llm_error,
        "audit_sequence_num": entry.sequence_num,
    }


@router.get("/insights/tier-analysis")
def tier_analysis(range: str = "30d", db: Session = Depends(get_db)):
    """
    Same shape again, but for the customer-tiering thresholds themselves
    (app/tiering.py) - the engagement-score thresholds, the score
    component calibration, and the risk gate. Every recommendation this
    produces is flagged with is_tier_threshold=true in the response, since
    changing these affects how every customer's tier is computed, not just
    future behavior - the frontend is expected to warn distinctly for these.
    """
    if range not in RANGE_PRESETS:
        raise HTTPException(400, f"range must be one of {list(RANGE_PRESETS)}")

    analysis_input = build_tier_analysis_input(db, range)

    summary = ""
    recommendations: list[dict] = []
    llm_error = None
    try:
        result = generate_tier_recommendations(analysis_input)
        summary = result["summary"]
        recommendations = result["recommendations"]
    except TierInsightsLLMError as e:
        llm_error = str(e)

    for rec in recommendations:
        rec["is_tier_threshold"] = True

    entry = write_audit_entry(
        db,
        action_type="tier_policy_recommendation_generated",
        details={
            "range": analysis_input["range"],
            "config_snapshot": analysis_input["config_snapshot"],
            "tier_distribution": analysis_input["tier_distribution"],
            "tier_wise_performance": analysis_input["tier_wise_performance"],
            "score_distribution": analysis_input["score_distribution"],
            "near_miss_customers": analysis_input["near_miss_customers"],
            "risk_flag_redemption": analysis_input["risk_flag_redemption"],
            "dormant_accounts_by_tier": analysis_input["dormant_accounts_by_tier"],
            "patterns": analysis_input["patterns"],
            "summary": summary,
            "recommendations": recommendations,
            "llm_error": llm_error,
        },
    )

    return {
        **analysis_input,
        "summary": summary,
        "recommendations": recommendations,
        "llm_error": llm_error,
        "audit_sequence_num": entry.sequence_num,
    }


@router.get("/insights/tier-config")
def tier_config(db: Session = Depends(get_db)):
    """
    Lightweight companion to /insights/tier-analysis for the customer-facing
    "how tiers work" explainer page (/tiers on the frontend). Deliberately
    NOT the same endpoint: this one does no LLM call and no range-scoped
    aggregation, just the current runtime_flags values plus the (fixed)
    engagement-score weights - cheap enough to load on every page view,
    unlike the full analysis above.
    """
    return {
        "engagement_weights": ENGAGEMENT_WEIGHTS,
        "tier_thresholds": tier_threshold_snapshot(),
        "tier_distribution": tier_distribution(db),
        "incentive_config": incentive_config_snapshot(),
    }


class ApplySuggestionRequest(BaseModel):
    param: str
    suggested_value: str
    rationale: str | None = None
    supporting_metric: str | None = None
    analysis_sequence_num: int | None = None


@router.post("/insights/apply-suggestion")
def apply_suggestion(req: ApplySuggestionRequest, db: Session = Depends(get_db)):
    """
    Applies one recommendation from EITHER analysis - in-memory only, for
    this running server (via app/runtime_flags.py, same mechanism as
    /debug/toggle-llm-failure). Does NOT edit .env, so it will not survive
    a restart unless someone updates .env by hand too. That's a deliberate
    scope limit, not an oversight: auto-rewriting the merchant's own config
    file from a request handler is a materially bigger, riskier feature
    than "let a human-approved suggestion take effect for the rest of this
    session" - the merchant explicitly clicking Implement is what makes
    this human-gated, same as everywhere else in this project the LLM's
    output only ever executes after a human/config-driven gate, never on
    its own.
    """
    if req.param not in _ALL_VALID_PARAMS:
        raise HTTPException(400, f"'{req.param}' is not an applicable parameter")

    if req.param == "casual_tier_incentive_eligible":
        if req.suggested_value not in ("true", "false"):
            raise HTTPException(
                400, "suggested_value must be 'true' or 'false' for casual_tier_incentive_eligible"
            )
        previous_value = "casual" in runtime_flags.get_incentive_eligible_tiers()
        runtime_flags.set_casual_tier_incentive_eligible(req.suggested_value == "true")
        new_value = "casual" in runtime_flags.get_incentive_eligible_tiers()

    elif req.param == "confidence_threshold":
        try:
            parsed = float(req.suggested_value)
        except ValueError:
            raise HTTPException(400, f"suggested_value '{req.suggested_value}' is not a valid decimal")
        if not (0.0 <= parsed <= 1.0):
            raise HTTPException(400, "confidence_threshold must be between 0 and 1")
        previous_value = runtime_flags.get_confidence_threshold()
        runtime_flags.set_confidence_threshold(parsed)
        new_value = runtime_flags.get_confidence_threshold()

    elif req.param in TIER_RATE_PARAMS or req.param in TIER_FLOAT_PARAMS:
        try:
            parsed = float(req.suggested_value)
        except ValueError:
            raise HTTPException(400, f"suggested_value '{req.suggested_value}' is not a valid decimal")
        if req.param in TIER_RATE_PARAMS and not (0.0 <= parsed <= 1.0):
            raise HTTPException(400, f"{req.param} must be between 0 and 1")
        if req.param in TIER_FLOAT_PARAMS and parsed <= 0:
            raise HTTPException(400, f"{req.param} must be greater than 0")
        _rate_setters = {
            "tier_risk_attributable_failure_rate": (
                runtime_flags.get_tier_risk_attributable_failure_rate,
                runtime_flags.set_tier_risk_attributable_failure_rate,
            ),
            "tier_risk_cancel_rate": (
                runtime_flags.get_tier_risk_cancel_rate, runtime_flags.set_tier_risk_cancel_rate,
            ),
            "tier_target_orders_per_month": (
                runtime_flags.get_tier_target_orders_per_month,
                runtime_flags.set_tier_target_orders_per_month,
            ),
        }
        getter, setter = _rate_setters[req.param]
        previous_value = getter()
        setter(parsed)
        new_value = getter()

    else:
        getter, setter = _NUMERIC_PARAMS[req.param]
        try:
            parsed = int(req.suggested_value)
        except ValueError:
            raise HTTPException(
                400, f"suggested_value '{req.suggested_value}' is not a valid integer for {req.param}"
            )
        floor = 1 if req.param in (
            "tier_min_attempts_for_loyal", "tier_min_attempts_for_regular", "tier_risk_min_attempts",
        ) else 0
        if parsed < floor:
            raise HTTPException(400, f"suggested_value must be >= {floor}")
        if req.param in ("tier_loyal_score", "tier_regular_score") and not (0 <= parsed <= 100):
            raise HTTPException(400, f"{req.param} is a 0-100 engagement score")
        _check_ordering(req.param, parsed)
        previous_value = getter()
        setter(parsed)
        new_value = getter()

    is_tier_threshold = req.param in TIER_THRESHOLD_PARAMS

    write_audit_entry(
        db,
        action_type="policy_recommendation_applied",
        details={
            "param": req.param,
            "previous_value": previous_value,
            "new_value": new_value,
            "rationale": req.rationale,
            "supporting_metric": req.supporting_metric,
            "analysis_sequence_num": req.analysis_sequence_num,
            "is_tier_threshold": is_tier_threshold,
        },
    )

    note = "Applied in-memory for this server only - update .env too if you want it to survive a restart."
    if is_tier_threshold:
        note += (
            " This changes how tiers are COMPUTED, not any existing customer's stored tier - "
            "nobody is re-evaluated until you explicitly run a re-evaluation "
            "(see /insights/tier-reevaluation-preview and /insights/tier-reevaluation-commit)."
        )

    return {
        "param": req.param,
        "previous_value": previous_value,
        "new_value": new_value,
        "applied": True,
        "is_tier_threshold": is_tier_threshold,
        "note": note,
    }


@router.get("/insights/tier-reevaluation-preview")
def tier_reevaluation_preview(param: str | None = None, value: str | None = None, db: Session = Depends(get_db)):
    """
    Read-only. Recomputes what EVERY customer's tier would be, compares to
    what's actually stored, and returns only the diff - nothing is written.

    With no query params: previews against the CURRENT (live) thresholds -
    useful right after a real apply, to confirm what it did.

    With param + value given: previews a HYPOTHETICAL threshold that has
    NOT been applied yet - nothing in runtime_flags is touched. This is
    what powers "preview before you commit to anything": a merchant can
    see the exact customer-level impact of a recommendation before the
    threshold itself is ever changed for real.
    """
    overrides = None
    if param is not None or value is not None:
        if param is None or value is None:
            raise HTTPException(400, "param and value must be given together")
        if param not in TIER_THRESHOLD_PARAMS:
            raise HTTPException(400, f"'{param}' is not a tiering threshold")
        try:
            parsed = (
                float(value) if param in (TIER_RATE_PARAMS | TIER_FLOAT_PARAMS) else int(value)
            )
        except ValueError:
            raise HTTPException(400, f"value '{value}' is not valid for {param}")
        overrides = {param: parsed}

    customers = db.query(Customer).all()
    moves: dict[str, list[dict]] = {}
    unchanged = 0
    for c in customers:
        proposed = compute_tier(db, c.id, overrides=overrides)
        if proposed.value == c.tier.value:
            unchanged += 1
            continue
        key = f"{c.tier.value} -> {proposed.value}"
        moves.setdefault(key, []).append({"id": c.id, "email": c.email, "name": c.name})

    return {
        "total_customers": len(customers),
        "unchanged": unchanged,
        "moves": {k: {"count": len(v), "customers": v} for k, v in moves.items()},
    }


@router.post("/insights/tier-reevaluation-commit")
def tier_reevaluation_commit(db: Session = Depends(get_db)):
    """
    Actually recomputes and persists every customer's tier under the
    current thresholds. Each customer whose tier actually changes gets a
    real tier_changed audit entry, via the same refresh_tier() hook every
    other tier change goes through - no separate audit logic needed here,
    this just calls it at volume. This is only ever reached from the
    frontend AFTER a merchant has already previewed the exact impact via
    /insights/tier-reevaluation-preview and explicitly clicked Apply -
    nothing commits on its own.
    """
    customers = db.query(Customer).all()
    changed = 0
    for c in customers:
        before = c.tier
        refresh_tier(db, c)
        if c.tier != before:
            changed += 1

    write_audit_entry(
        db,
        action_type="tier_reevaluation_committed",
        details={"total_customers": len(customers), "changed": changed},
    )

    return {"total_customers": len(customers), "changed": changed}

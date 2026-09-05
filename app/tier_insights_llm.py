"""
Turns app/tier_insights.py's aggregated output into recommendations for
the engagement-score tiering model's constants. Same schema-constrained pattern as
insights_llm.py / recovery_insights_llm.py.

Every parameter this module can recommend changes HOW EVERY CUSTOMER'S
TIER IS COMPUTED - a fundamentally bigger blast radius than any other
recommendation in this project (those only ever change future behavior;
these change what a stored fact about every customer IS, once re-evaluated).
The router is responsible for surfacing that as a visible warning - this
module's job is just bounded, evidence-based recommendations.
"""
import json

import httpx

from app.config import settings

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# How many individual customers to name per near-miss/redeemed list in the
# PROMPT specifically - the merchant-facing API response and dashboard get
# every customer (that's the point of those lists), but a policy
# recommendation only needs COUNTS to reason about, not every email. A
# customer base with a few dozen near-miss customers was measured pushing
# this prompt over Groq's free-tier tokens-per-minute limit (a real 413 in
# testing, at 8648/8000 tokens) - same reasoning as
# insights.py's REASONING_SAMPLES_PER_BUCKET: enough for qualitative
# grounding, not the whole corpus.
_LLM_CUSTOMER_SAMPLE = 5


def _summarize_customer_list(customers: list[dict]) -> dict:
    return {
        "count": len(customers),
        "sample": [
            {k: v for k, v in c.items() if k in ("name", "email", "engagement_score", "next_tier", "trigger")}
            for c in customers[:_LLM_CUSTOMER_SAMPLE]
        ],
    }


def _summarize_for_prompt(analysis_input: dict) -> dict:
    """A version of near_miss_customers/risk_flag_redemption capped for the
    LLM specifically - see _LLM_CUSTOMER_SAMPLE. Everything else passes
    through unchanged; these are the only two sections whose size scales
    with the customer base rather than staying bounded."""
    nm = analysis_input["near_miss_customers"]
    rr = analysis_input["risk_flag_redemption"]
    return {
        "near_miss_customers": {
            "close_to_promotion": _summarize_customer_list(nm["close_to_promotion"]),
            "close_on_score": _summarize_customer_list(nm["close_on_score"]),
            "close_to_risk": _summarize_customer_list(nm["close_to_risk"]),
        },
        "risk_flag_redemption": {
            "total_flagged_permanently": rr["total_flagged_permanently"],
            "redeemed_since_count": rr["redeemed_since_count"],
            "redeemed_customers_sample": _summarize_customer_list(rr["redeemed_customers"])["sample"],
        },
    }

VALID_PARAMS = [
    "tier_loyal_score",
    "tier_regular_score",
    "tier_min_attempts_for_loyal",
    "tier_min_attempts_for_regular",
    "tier_target_orders_per_month",
    "tier_target_aov_inr",
    "tier_recency_window_days",
    "tier_behavior_window_days",
    "tier_risk_min_attempts",
    "tier_risk_attributable_failure_rate",
    "tier_risk_cancel_rate",
]

# Params expressed as a 0-1 rate, for the router's cast/validation and for
# telling the model what shape of number to return.
RATE_PARAMS = {
    "tier_risk_attributable_failure_rate",
    "tier_risk_cancel_rate",
}
# Params expressed as a decimal that is NOT bounded by 1.
FLOAT_PARAMS = {"tier_target_orders_per_month"}

TOOL_NAME = "record_tier_threshold_recommendations"
TOOL_DESCRIPTION = "Record recommendations for the merchant's customer-tiering thresholds."
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-2 sentence plain-language overview of what the data shows.",
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "param": {
                        "type": "string",
                        "enum": VALID_PARAMS,
                        "description": "Which whitelisted tiering threshold this changes. Never propose anything outside this list.",
                    },
                    "current_value": {"type": "string"},
                    "suggested_value": {
                        "type": "string",
                        "description": (
                            "A decimal between 0 and 1 for tier_risk_attributable_failure_rate "
                            "and tier_risk_cancel_rate. A positive decimal for "
                            "tier_target_orders_per_month. A positive integer for everything "
                            "else. Scores (tier_loyal_score, tier_regular_score) are integers "
                            "on a 0-100 scale."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why, citing specific numbers from the input (e.g. how many near-miss customers, how many redeemed risk-flagged customers). Must name numbers.",
                    },
                    "supporting_metric": {"type": "string"},
                },
                "required": ["param", "current_value", "suggested_value", "rationale", "supporting_metric"],
            },
        },
    },
    "required": ["summary", "recommendations"],
}


class TierInsightsLLMError(Exception):
    pass


def _build_prompt(analysis_input: dict) -> str:
    prompt_summary = _summarize_for_prompt(analysis_input)

    def section(key):
        # near_miss_customers/risk_flag_redemption are capped for the
        # prompt specifically (see _summarize_for_prompt) - everything
        # else is small and bounded regardless of customer-base size, so
        # passes through from analysis_input unchanged.
        if key in prompt_summary:
            return json.dumps(prompt_summary[key], indent=2)
        return json.dumps(analysis_input[key], indent=2)

    return f"""You are analyzing a merchant's customer-tiering model (what separates
New/Casual/Regular/Loyal/Risk customers) and recommending changes. These
settings are foundational - they decide who the rest of the recovery
system treats as worth spending on - so recommend a change only when the
evidence is real, not to have something to say.

HOW TIERS WORK HERE, so your recommendations make sense:
- Every customer gets an engagement score from 0 to 100, built from five
  weighted components: cart completion (30), purchase frequency (25),
  average order value (20), recency (15), and whether they respond to
  recovery nudges (10).
- Tier is then: score >= tier_loyal_score AND at least
  tier_min_attempts_for_loyal purchases -> Loyal. Else score >=
  tier_regular_score AND at least tier_min_attempts_for_regular -> Regular.
  Else Casual. No purchase history at all -> New.
- Risk is checked FIRST and is NOT a low score - it is a separate gate
  (a fraud risk_block flag, too high a rate of customer-attributable
  payment failures, or too high a cancellation rate).
- Payment failures caused by the gateway, the bank, or unknown errors are
  deliberately excluded from all of this. Only customer-attributable
  failures (expired card, insufficient funds, invalid card, failed
  authentication, fraud block, cancelled at the payment screen) count.
- Retries are collapsed: three tries at one purchase is ONE purchase
  attempt, not three.

Time window for activity-based sections: {analysis_input["range"]} (since {analysis_input["since"] or "the beginning"})
(Tier distribution, score distribution, near-miss customers, and risk-flag
redemption are always current-state snapshots, not scoped to the window.)

Current settings (config_snapshot):
{section("config_snapshot")}

How many customers are in each tier right now (tier_distribution):
{section("tier_distribution")}

How engagement scores are spread across the customer base
(score_distribution) - use this to judge whether a threshold sits in a
sensible gap or straight through a cluster of customers:
{section("score_distribution")}

Per-tier business performance in this window (tier_wise_performance):
{section("tier_wise_performance")}

Customers sitting right at a boundary (near_miss_customers) -
close_to_promotion (score is already there, just short on purchase count),
close_on_score (have the purchases, within 10 points of the score needed),
close_to_risk (one more bad event would trip the risk gate). Each list is
capped to "count" (the real total - cite THIS number) plus a small "sample"
of individual customers for qualitative color, not every customer:
{section("near_miss_customers")}

Customers permanently stuck in risk tier from a single risk_block flag,
and how many have paid successfully since (risk_flag_redemption) -
redeemed_customers_sample is a few examples, redeemed_since_count is the
real total, cite that one:
{section("risk_flag_redemption")}

% of each tier with no orders in this window (dormant_accounts_by_tier):
{section("dormant_accounts_by_tier")}

Plain-English patterns already shown to the merchant on this page, derived
from the exact same sections above (patterns) - not every one maps to a
whitelisted param (some are meant for the merchant to act on directly, e.g.
reaching out to near-miss customers), but your summary and any
recommendation rationale must be CONSISTENT with these, not contradict them:
{section("patterns")}

Rules you MUST follow:
1. You may ONLY recommend changing a parameter in this exact list: {VALID_PARAMS}.
2. Every recommendation's rationale must cite an actual number from the
   sections above - e.g. "14 of 20 risk-flagged customers have paid
   successfully since being flagged" is a real rationale; "this seems
   too strict" is not.
3. near_miss_customers and dormant_accounts_by_tier can have very small
   counts - do not treat a handful of customers as a strong trend. If the
   numbers are small, say so explicitly rather than recommending a change
   off a thin sample.
4. If the data doesn't clearly support a change, return an empty
   recommendations list and say so in the summary. These settings affect
   every customer's tier at once - a weak recommendation here does more
   damage than a weak one for a routine parameter, so the bar for
   recommending anything is higher, not lower.
5. Value formats: {sorted(RATE_PARAMS)} must be a decimal between 0 and 1.
   tier_target_orders_per_month must be a positive decimal. Everything
   else must be a positive integer, and the two score params are on a
   0-100 scale.
6. tier_regular_score must stay strictly below tier_loyal_score, and
   tier_min_attempts_for_regular at or below tier_min_attempts_for_loyal.
   A recommendation that would invert either ordering will be rejected.

Call {TOOL_NAME} with your findings."""


def _call_groq(prompt: str) -> dict:
    if not settings.groq_api_key:
        raise TierInsightsLLMError("No GROQ_API_KEY configured")

    body = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "parameters": TOOL_PARAMETERS,
            },
        }],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        "max_tokens": 1800,
    }

    try:
        resp = httpx.post(
            GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=25.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise TierInsightsLLMError(f"Groq API call failed: {e}") from e

    data = resp.json()
    try:
        tool_call = data["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(tool_call["function"]["arguments"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise TierInsightsLLMError(f"Groq response missing usable tool call: {e}") from e

    return args


def generate_recommendations(analysis_input: dict) -> dict:
    prompt = _build_prompt(analysis_input)
    result = _call_groq(prompt)

    recommendations = result.get("recommendations", [])
    if not isinstance(recommendations, list):
        raise TierInsightsLLMError("Groq response 'recommendations' was not a list")

    valid = [r for r in recommendations if r.get("param") in VALID_PARAMS]

    return {"summary": result.get("summary", ""), "recommendations": valid}

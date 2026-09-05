"""
Turns the aggregated output of insights.py into recommendations, via the
same schema-constrained Groq function-calling pattern llm_agent.py uses
for per-event decisions - bounded output, not a free-text policy essay.

Unlike llm_agent.py, there is no deterministic fallback here: there's no
"rules-engine" version of a policy recommendation to fall back to. A
failure here just means the analysis isn't available right now - callers
should surface that plainly rather than fabricate a recommendation.
"""
import json

import httpx

from app.config import settings

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# The ONLY parameters a recommendation is allowed to name. Mirrors
# insights.config_snapshot()'s whitelist, plus the one non-numeric toggle
# (casual-tier eligibility). NEW and RISK deliberately have no entry here
# at all - there is no schema value that could ever request them, not just
# a prompt instruction not to.
#
# The old flat incentive_discount_pct and incentive_max_per_customer_30d
# are gone: both are per-tier now. What the LLM can move is the BAND a
# tier's discounts live in, and how many offers that tier gets in 30 days.
# It still never picks the discount an individual customer sees - that
# comes from where their engagement score sits inside the band
# (tiering.incentive_pct_for_customer), so the rule "the LLM never chooses
# a customer's discount" is unchanged; it now applies to a formula's bounds
# instead of a single constant.
#
# Band bounds are ordering-checked in insights_router._ORDERED_PAIRS, so a
# recommendation can't invert the ladder by giving CASUAL a wider band than
# LOYAL. NEW and RISK deliberately have no entry here at all - there is no
# schema value that could ever request them, not just a prompt instruction
# not to.
VALID_PARAMS = [
    "incentive_max_order_value_casual",
    "incentive_max_order_value_regular",
    "incentive_max_order_value_loyal",
    "nudge_expiry_hours",
    "casual_tier_incentive_eligible",
    "incentive_pct_casual_min",
    "incentive_pct_casual_max",
    "incentive_pct_regular_min",
    "incentive_pct_regular_max",
    "incentive_pct_loyal_min",
    "incentive_pct_loyal_max",
    "incentive_max_per_30d_casual",
    "incentive_max_per_30d_regular",
    "incentive_max_per_30d_loyal",
]

TOOL_NAME = "record_incentive_recommendations"
TOOL_DESCRIPTION = "Record policy recommendations for the merchant's cart-event incentive settings."
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
                        "description": "Which whitelisted parameter this recommendation changes. Never propose anything outside this list.",
                    },
                    "current_value": {
                        "type": "string",
                        "description": "The parameter's current value - look it up directly at "
                                        "config_snapshot.current_values[param], never guess or infer it. "
                                        "Never write 'unknown' - if you cannot find it there, do not "
                                        "recommend that param at all.",
                    },
                    "suggested_value": {
                        "type": "string",
                        "description": "The suggested new value - an actual number (e.g. '3000'), never "
                                        "a vague direction word like 'increase' or 'lower'. For "
                                        "casual_tier_incentive_eligible, must be exactly 'true' or 'false'.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why, citing the specific metric(s) that support this. Must name numbers from the input, not generic reasoning.",
                    },
                    "supporting_metric": {
                        "type": "string",
                        "description": "The specific bucket/metric this is based on, e.g. 'loyal/explicit_cancel net_recovered_inr'.",
                    },
                },
                "required": ["param", "current_value", "suggested_value", "rationale", "supporting_metric"],
            },
        },
    },
    "required": ["summary", "recommendations"],
}


class InsightsLLMError(Exception):
    pass


def _build_prompt(analysis_input: dict) -> str:
    buckets_text = json.dumps(analysis_input["buckets"], indent=2)
    config_text = json.dumps(analysis_input["config_snapshot"], indent=2)
    samples_text = json.dumps(analysis_input["reasoning_samples"], indent=2)
    overview_text = json.dumps(analysis_input["overview"], indent=2)
    patterns_text = json.dumps(analysis_input["patterns"], indent=2)

    return f"""You are analyzing a merchant's cart-abandonment incentive program (discounts
offered to customers who abandon or cancel their cart) and recommending
policy changes. This is a POLICY-level review, not a single-customer
decision.

Time window: {analysis_input["range"]} (since {analysis_input["since"] or "the beginning"})

Current settings (config_snapshot):
{config_text}

Synthesized overview - one row per tier plus an overall total, already
aggregated from the buckets below (use this first; it's the same numbers,
just rolled up so you don't have to re-derive rates yourself):
- tier_leaderboard: per tier, redemption rate vs. baseline_conversion_rate_pct
  (what a plain reminder/resume-link converts at for the SAME tier, no
  discount), lift_pct (the difference - the actual "did the discount beat
  doing nothing" number), avg_incentive_pct_given vs. that tier's
  incentive_pct_band, and net_recovered_inr.
- best_tier / worst_tier: by net_recovered_inr, restricted to tiers with
  enough sample to trust (see low_sample below) - null if nothing qualifies.
- blocking_summary: which tiers are having offers skipped and why
  (freq/amount/tier-gate). Check this BEFORE recommending a cap or
  frequency change - if a tier's blocked count this range is 0, its cap
  isn't the constraint, and raising it would do nothing.
{overview_text}

Plain-English patterns already shown to the merchant on this page, derived
from the exact same overview numbers above (patterns):
{patterns_text}
Your summary and each recommendation's rationale must be CONSISTENT with
these - do not contradict them, and do not just restate one verbatim as
the summary. Use them as the starting point for "so what should change",
then name the specific whitelisted parameter and value.

Per-bucket metrics, grouped by customer tier x abandonment type
(buckets) - the same data at finer grain, if you need the silent_abandon
vs explicit_cancel split for a specific tier:
{buckets_text}

A few real logged reasoning notes, for context (reasoning_samples):
{samples_text}

Rules you MUST follow:
1. You may ONLY recommend changing a parameter in this exact list: {VALID_PARAMS}.
   Never invent a parameter name, and never recommend anything for the
   "new" or "risk" customer tiers - they are not eligible for incentives
   under any circumstance and are outside the scope of this tool.
1b. current_value MUST come from config_snapshot.current_values[param] -
   a direct lookup, not a guess. suggested_value MUST be an actual number
   for every param except casual_tier_incentive_eligible (e.g. "3000",
   never "increase", "higher", "lower", or any other non-numeric word).
   If you cannot find a param's current value in current_values, do not
   recommend changing it.
2. Every recommendation's rationale must cite an actual number from the
   overview or buckets above (e.g. "net_recovered_inr of X across Y
   events", "lift_pct of Z points"). Do not write generic-sounding
   justifications with no number attached.
3. If a bucket or tier has "low_sample": true, do NOT base a
   recommendation primarily on it - either skip it or explicitly say the
   sample is too small to act on yet. Do not present a low-sample number
   as a trend, and do not name a low-sample tier as best_tier/worst_tier
   even if the overview left one null.
4. If the data doesn't clearly support any change, return an empty
   recommendations list and say so in the summary. Do not invent a
   recommendation just to have something to say.
5. For casual_tier_incentive_eligible, suggested_value must be exactly
   "true" or "false" - true means "add casual to incentive_eligible_tiers",
   false means "remove it" (only meaningful if it's currently there).
6. Discount percentages are NOT tunable here. Each tier has a fixed
   discount band (casual 0-10%, regular 10-20%, loyal 20-30%) and a
   customer's exact % inside their band is derived from their engagement
   score. Do not recommend a discount rate - it is not in the list above.
   You MAY recommend widening/narrowing/shifting a band's min or max if
   avg_incentive_pct_given clustering against the band (e.g. always near
   the floor) supports it.

Call {TOOL_NAME} with your findings."""


def _call_groq(prompt: str) -> dict:
    if not settings.groq_api_key:
        raise InsightsLLMError("No GROQ_API_KEY configured")

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
        "max_tokens": 1500,
    }

    try:
        resp = httpx.post(
            GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=20.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise InsightsLLMError(f"Groq API call failed: {e}") from e

    data = resp.json()
    try:
        tool_call = data["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(tool_call["function"]["arguments"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise InsightsLLMError(f"Groq response missing usable tool call: {e}") from e

    return args


def _valid_suggested_value(param: str, suggested_value) -> bool:
    """Every param except the boolean toggle must be a real number - never
    a direction word like "increase"/"lower". Checked here as a safety
    net independent of the prompt instructions above: those reduce how
    often the LLM gets this wrong, this guarantees a malformed value can
    never reach the merchant's screen even so."""
    if param == "casual_tier_incentive_eligible":
        return suggested_value in ("true", "false")
    try:
        float(suggested_value)
        return True
    except (TypeError, ValueError):
        return False


def generate_recommendations(analysis_input: dict) -> dict:
    """Returns {"summary": str, "recommendations": [...]}. Raises
    InsightsLLMError on any failure - callers must not fabricate a
    result."""
    prompt = _build_prompt(analysis_input)
    result = _call_groq(prompt)

    recommendations = result.get("recommendations", [])
    if not isinstance(recommendations, list):
        raise InsightsLLMError("Groq response 'recommendations' was not a list")

    # Defense in depth: the schema enum already constrains this, but don't
    # trust it blindly - same posture llm_agent.py takes on the action enum.
    valid = []
    for rec in recommendations:
        param = rec.get("param")
        if param not in VALID_PARAMS:
            continue  # silently drop, don't let an out-of-schema param through
        if not _valid_suggested_value(param, rec.get("suggested_value")):
            # A merchant-facing "Implement" button that fails to parse (e.g.
            # suggested_value="increase" instead of a real number) is worse
            # than no recommendation at all - drop it here rather than let
            # apply-suggestion's int()/float() parsing surface the error to
            # the merchant as a broken button.
            continue
        valid.append(rec)

    return {
        "summary": result.get("summary", ""),
        "recommendations": valid,
    }

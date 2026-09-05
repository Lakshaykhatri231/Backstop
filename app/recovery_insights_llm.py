"""
Turns app/recovery_insights.py's aggregated output into recommendations,
via the same schema-constrained Groq function-calling pattern as
insights_llm.py. Separate module because the whitelist and metrics are
entirely different (retry/escalation/confidence ops params, not incentive
economics or tiering thresholds), even though the shape is identical.

The analysis it reads covers the whole loss funnel (abandoned carts,
cancellations, failed payments, give-ups), but the whitelist here is
deliberately narrow - the cart-side knobs belong to Incentive Analysis and
the tier knobs to Tier Analysis, and two modals recommending the same
parameter would be a genuinely confusing thing to put in front of a
merchant. The cart numbers are here for DIAGNOSIS; if the strongest lever
is somewhere else, the model is told to say so in the summary instead of
inventing a parameter.
"""
import json

import httpx

from app.config import settings

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

VALID_PARAMS = [
    "confidence_threshold",
    "max_auto_retries",
    "high_value_amount_inr",
]

# Rows of the per-reason / per-attempt tables to hand the model. These grow
# with traffic, and an oversized prompt is not a theoretical problem here:
# Tier Analysis hit a real 413 (8648 tokens against a free-tier 8000 TPM
# cap) from an uncapped customer list. Counts are what a policy
# recommendation actually reasons about; the long tail adds bytes, not
# judgement.
_LLM_ROW_SAMPLE = 8

TOOL_NAME = "record_recovery_policy_recommendations"
TOOL_DESCRIPTION = "Record policy recommendations for the merchant's loss-recovery handling."
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 sentences, plain language, on where this merchant is losing money "
                            "and what the biggest lever is - even if that lever is not a parameter "
                            "you can change here.",
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
                                        "config_snapshot[param], never guess. Never write 'unknown'.",
                    },
                    "suggested_value": {
                        "type": "string",
                        "description": "An actual value, never a direction word like 'increase'. A "
                                        "decimal between 0 and 1 for confidence_threshold; a "
                                        "non-negative integer for max_auto_retries and "
                                        "high_value_amount_inr.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why, citing specific numbers from the input. Must name numbers, not generic reasoning.",
                    },
                    "supporting_metric": {
                        "type": "string",
                        "description": "The specific metric/section this is based on, e.g. 'retry_effectiveness.ladder attempt 3'.",
                    },
                },
                "required": ["param", "current_value", "suggested_value", "rationale", "supporting_metric"],
            },
        },
    },
    "required": ["summary", "recommendations"],
}


class RecoveryInsightsLLMError(Exception):
    pass


def _valid_suggested_value(param: str, suggested_value) -> bool:
    """Safety net independent of the prompt: a merchant-facing Implement
    button that fails to parse is worse than no recommendation at all."""
    try:
        value = float(suggested_value)
    except (TypeError, ValueError):
        return False
    if param == "confidence_threshold":
        return 0.0 <= value <= 1.0
    return value >= 0


def _trim(rows, limit=_LLM_ROW_SAMPLE):
    if isinstance(rows, list) and len(rows) > limit:
        return rows[:limit]
    return rows


def _build_prompt(analysis_input: dict) -> str:
    def section(key, trim=False):
        value = analysis_input[key]
        if trim:
            value = _trim(value)
        return json.dumps(value, indent=2)

    leak = analysis_input["leak_summary"]

    return f"""You are analyzing where a merchant's online store loses revenue, and
recommending policy changes to the automated recovery system.

There are three ways money leaks out, all reported the same way below so
they can be compared:
- "silent_abandon": the customer left items in the cart and went quiet.
- "explicit_cancel": the customer actively deleted their cart.
- "payment_failure": checkout was attempted and the payment was declined.
  These are grouped into RUNS - three retries at one purchase is one run,
  not three.

Each of those resolves as recovered (the money came back), still_open
(recovery is still in progress), lost (written off), or - for cart signals
only - handed_to_checkout, meaning the customer did come back and check
out but the payment then failed, so that money is counted in the
payment_failure row instead. It is never counted twice.

A "give-up" is a customer explicitly abandoning a failed payment. It is a
RESOLUTION of a payment_failure run, not a fourth leak - its money is
already inside the payment_failure row's lost figure. Do not add it on top.

Time window: {analysis_input["range"]} (since {analysis_input["since"] or "the beginning"})

Current settings you can change (config_snapshot) - read current_value for
any recommendation straight out of this object:
{section("config_snapshot")}

Where the money is leaking (leak_summary):
{section("leak_summary")}

Why payments fail, how recoverable each reason is, and what it costs
(failure_reason_analysis) - sorted by money lost:
{section("failure_reason_analysis", trim=True)}

Whether extra retries actually buy anything (retry_effectiveness) - of the
runs that reached attempt N, how many ever recovered. This is the direct
evidence for max_auto_retries:
{section("retry_effectiveness")}

When customers give up, and how hard they tried first (give_up_analysis):
{section("give_up_analysis")}

Escalations that were purely amount-triggered vs an actual red flag - the
evidence for high_value_amount_inr (escalation_amount_analysis):
{section("escalation_amount_analysis")}

How often the confidence gate overrode the chosen action, per pipeline -
the evidence for confidence_threshold (confidence_override_rates):
{section("confidence_override_rates")}

Whether the real AI decided or it fell back to the plain rulebook
(agent_reliability):
{section("agent_reliability")}

Customers with more than one failed run, by tier (repeat_offenders_by_tier):
{section("repeat_offenders_by_tier")}

Plain-English patterns already shown to the merchant on this page, derived
from the exact same numbers (patterns). Your summary must be CONSISTENT
with these and must not contradict them - build on them rather than
restating one verbatim:
{section("patterns")}

Rules you MUST follow:
1. You may ONLY recommend changing a parameter in this exact list: {VALID_PARAMS}.
   Never invent a parameter name.
2. current_value must come from config_snapshot[param] - a direct lookup,
   never a guess, never "unknown". suggested_value must be an actual
   number, never a direction word like "increase" or "lower".
3. Every recommendation's rationale must cite an actual number from the
   sections above. Do not write generic-sounding justifications with no
   number attached.
4. Any group with "low_sample": true, or a very small count, must not be
   the primary basis for a recommendation - either skip it or say
   explicitly that the sample is too small to act on yet.
5. confidence_threshold affects BOTH the payment-failure and the abandoned-
   checkout pipeline - if you recommend changing it, account for both.
6. The cart-side numbers (silent_abandon, explicit_cancel) are here so you
   can diagnose where the losses actually are. You CANNOT tune discounts,
   offer limits or tier thresholds from here. If the strongest lever is an
   incentive setting, say so in the summary and point the merchant at the
   Incentive Analysis screen; if it's a tiering threshold, point at Tier
   Analysis. Never express it as a recommendation object.
7. If the data doesn't clearly support any change, return an empty
   recommendations list and say so in the summary. Do not invent a
   recommendation just to have something to say.

Call {TOOL_NAME} with your findings."""


def _call_groq(prompt: str) -> dict:
    if not settings.groq_api_key:
        raise RecoveryInsightsLLMError("No GROQ_API_KEY configured")

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
        raise RecoveryInsightsLLMError(f"Groq API call failed: {e}") from e

    data = resp.json()
    try:
        tool_call = data["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(tool_call["function"]["arguments"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RecoveryInsightsLLMError(f"Groq response missing usable tool call: {e}") from e

    return args


def generate_recommendations(analysis_input: dict) -> dict:
    prompt = _build_prompt(analysis_input)
    result = _call_groq(prompt)

    recommendations = result.get("recommendations", [])
    if not isinstance(recommendations, list):
        raise RecoveryInsightsLLMError("Groq response 'recommendations' was not a list")

    valid = []
    for rec in recommendations:
        param = rec.get("param")
        if param not in VALID_PARAMS:
            continue
        if not _valid_suggested_value(param, rec.get("suggested_value")):
            continue
        valid.append(rec)

    return {"summary": result.get("summary", ""), "recommendations": valid}

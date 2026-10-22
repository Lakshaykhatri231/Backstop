"""
Recommendation layer for the Collections analysis (mandate retries, promises,
voice, receivables). Same contract as the other three LLM modules:

- the model may ONLY recommend a whitelisted parameter, with an actual number;
- there is NO fallback - if the call fails, recommendations is empty and
  llm_error is set (a made-up recommendation is worse than none);
- guards that do not depend on the model obeying the prompt are enforced in
  code afterwards: value shape, and - specific to this analysis - a
  recommendation is dropped if the evidence behind that setting is below the
  minimum sample (collections_insights.evidence_status).
"""
import json

import httpx

from app.collections_insights import EVIDENCE_METRIC
from app import groq_retry
from app.config import settings

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

VALID_PARAMS = list(EVIDENCE_METRIC)

# sanity ceilings: the router re-checks these on apply
PARAM_BOUNDS = {
    "mandate_max_retries": (0, 10),
    "mandate_retry_window_days": (1, 60),
    "promise_max_days_b2c": (1, 90),
    "promise_max_days_b2b": (1, 180),
    "promise_grace_days": (0, 14),
    "promise_max_broken": (1, 10),
    "voice_max_attempts_per_subject": (1, 10),
    "receivables_escalate_after_days": (1, 180),
}

TOOL_NAME = "record_collections_policy_recommendations"
TOOL_DESCRIPTION = "Record policy recommendations for the merchant's collections handling."
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 sentences, plain language, on how collections are performing "
                           "and the biggest lever - or that there is not enough data yet.",
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "param": {"type": "string", "enum": VALID_PARAMS,
                              "description": "Which whitelisted parameter to change. Never anything outside this list."},
                    "current_value": {"type": "string",
                                      "description": "Look it up at config_snapshot[param]; never guess."},
                    "suggested_value": {"type": "string",
                                        "description": "An actual non-negative integer, never a direction word."},
                    "rationale": {"type": "string",
                                  "description": "Why, citing specific numbers from the input."},
                    "supporting_metric": {"type": "string",
                                          "description": "The metric this is based on, e.g. 'mandate_attempts.ladder retry 3'."},
                },
                "required": ["param", "current_value", "suggested_value", "rationale", "supporting_metric"],
            },
        },
    },
    "required": ["summary", "recommendations"],
}


class CollectionsInsightsLLMError(Exception):
    pass


def _valid_value(param: str, suggested_value) -> bool:
    try:
        v = float(suggested_value)
    except (TypeError, ValueError):
        return False
    if v != int(v):
        return False
    lo, hi = PARAM_BOUNDS[param]
    return lo <= v <= hi


def _build_prompt(a: dict) -> str:
    def sec(key):
        return json.dumps(a[key], indent=2)

    return f"""You are analysing how a merchant collects money that has gone unpaid, and
recommending changes to the automated collections settings.

Four mechanisms are covered:
- Failed recurring debits ("mandate runs"): retried on a schedule; each run ends
  recovered or lost.
- Promises to pay: a customer gives a date; it ends kept or broken. A promise
  never moves money by itself.
- Automated voice calls about a debt; a call is "productive" if it produced a
  payment link or a promise.
- B2B invoices that go overdue and are chased on a ladder.

Time window: {a["range"]} (since {a["since"] or "the beginning"})
Minimum resolved cases a metric needs before it can support a change: {a["min_sample"]}

Current settings you can change (config_snapshot) - read current_value from here:
{sec("config_snapshot")}

Evidence sufficiency per setting (evidence). A setting whose "sufficient" is
false has NOT got enough data - you must not recommend changing it:
{sec("evidence")}

mandate_attempts - recovery rate per retry number (evidence for mandate_max_retries):
{sec("mandate_attempts")}

mandate_timing - days from first failure to recovery (evidence for mandate_retry_window_days):
{sec("mandate_timing")}

promises_by_horizon - kept rate by how far ahead the promise was, consumer (b2c) vs
business (b2b) (evidence for promise_max_days_b2c / promise_max_days_b2b):
{sec("promises_by_horizon")}

promise_late_payment - broken promises that were paid soon after (evidence for promise_grace_days):
{sec("promise_late_payment")}

promise_repeat - kept rate of 1st/2nd/3rd promise on one debt (evidence for promise_max_broken):
{sec("promise_repeat")}

voice_by_attempt - productive-call rate by call number (evidence for voice_max_attempts_per_subject):
{sec("voice_by_attempt")}

receivables_timing - when overdue invoices actually got paid (evidence for receivables_escalate_after_days):
{sec("receivables_timing")}

Supporting context. These explain WHY the numbers above look as they do. They have no setting of their
own and do NOT make a setting's evidence sufficient or insufficient (only "evidence" above decides that),
but you may cite them in a rationale, and should when they change what a setting ought to be:

mandate_failure_reasons - recovery by why the debit failed (a retry cannot fix every reason):
{sec("mandate_failure_reasons")}

receivables_outcomes - what became of ALL overdue invoices, including never-paid ones (receivables_timing
only sees the paid ones):
{sec("receivables_outcomes")}

chase_step_effectiveness - share of reminders followed by payment within 3 days, per step:
{sec("chase_step_effectiveness")}

link_conversion - real payment links by what they were for: paid vs expired:
{sec("link_conversion")}

promises_by_source - kept rate by where the promise came from:
{sec("promises_by_source")}

voice_outcomes - how calls end:
{sec("voice_outcomes")}

trend - this window vs the equal window before it (null for all time):
{sec("trend")}

Plain-English patterns already shown to the merchant, from these same numbers.
Your summary must be consistent with them:
{sec("patterns")}

Rules you MUST follow:
1. Only recommend a parameter from this exact list: {VALID_PARAMS}.
2. current_value comes from config_snapshot[param]; suggested_value is an actual
   integer within a sensible range, never a direction word.
3. Every rationale must cite an actual number from the sections above.
4. NEVER recommend a setting whose evidence.sufficient is false, and never base
   a recommendation on a group with fewer than 3 cases.
5. Contact guardrails (the calling window, consent, one call per day) are not
   tunable and are not in the list - never suggest touching them.
6. If the data does not clearly support a change, return an empty
   recommendations list and say so (including "not enough data yet").

Call {TOOL_NAME} with your findings."""


def _call_groq(prompt: str) -> dict:
    if not settings.groq_api_key:
        raise CollectionsInsightsLLMError("No GROQ_API_KEY configured")
    body = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "function", "function": {
            "name": TOOL_NAME, "description": TOOL_DESCRIPTION, "parameters": TOOL_PARAMETERS}}],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        "max_tokens": 1800,
    }
    try:
        resp = groq_retry.post(
            GROQ_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
            json=body, timeout=25.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise CollectionsInsightsLLMError(f"Groq API call failed: {e}") from e
    try:
        call = resp.json()["choices"][0]["message"]["tool_calls"][0]
        raw = call["function"]["arguments"]
        # Providers differ: arguments may arrive as a JSON string or already parsed.
        args = raw if isinstance(raw, dict) else json.loads(raw)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise CollectionsInsightsLLMError(f"Groq response missing usable tool call: {e}") from e
    if not isinstance(args, dict):
        raise CollectionsInsightsLLMError("Groq tool arguments were not an object")
    return args


def generate_recommendations(analysis_input: dict) -> dict:
    result = _call_groq(_build_prompt(analysis_input))
    recs = result.get("recommendations", [])
    if not isinstance(recs, list):
        raise CollectionsInsightsLLMError("Groq response 'recommendations' was not a list")

    evidence = analysis_input["evidence"]
    config = analysis_input["config_snapshot"]
    valid, dropped = [], []
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        param = rec.get("param")
        if param not in VALID_PARAMS:
            continue
        if not _valid_value(param, rec.get("suggested_value")):
            continue
        if not evidence[param]["sufficient"]:
            # Enforced here, not trusted to the prompt.
            dropped.append(param)
            continue
        valid.append({
            "param": param,
            # current_value is looked up, never taken from the model's text
            "current_value": str(config[param]),
            "suggested_value": str(int(float(rec["suggested_value"]))),
            "rationale": str(rec.get("rationale", "")),
            "supporting_metric": str(rec.get("supporting_metric", EVIDENCE_METRIC[param])),
        })
    summary = result.get("summary", "")
    return {"summary": summary if isinstance(summary, str) else "", "recommendations": valid,
            "dropped_for_insufficient_evidence": dropped}

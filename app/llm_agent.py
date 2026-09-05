"""
LLM-enriched decision layer, backed by Groq's free API.

Works for BOTH payment failure events and checkout abandonment events.
The rules engine provides the baseline decision; this layer enriches the
reasoning and may refine confidence — but the action is always constrained
to the AgentAction enum via tool-calling schema. The LLM enriches, never invents.

Graceful failure: any exception falls back to the rules-engine decision,
tagged in the audit log as "rules_engine_fallback".
"""
import json

import httpx

from app.config import settings
from app.models import FailureReason, AgentAction, EventType
from app.rules_engine import rule_based_decision, rule_based_dropoff_decision
from app.runtime_flags import is_llm_failure_forced

# Actions available to the LLM — excludes RULE_DEFAULT_FALLBACK (internal only)
VALID_ACTIONS = [a.value for a in AgentAction if a != AgentAction.RULE_DEFAULT_FALLBACK]

TOOL_NAME = "record_recovery_decision"
TOOL_DESCRIPTION = "Record the final recovery decision for a revenue-loss event."
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": VALID_ACTIONS,
            "description": "The recovery action to take. Must be chosen from this fixed list only.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Confidence in this decision, 0 to 1.",
        },
        "reasoning": {
            "type": "string",
            "description": "One or two sentence human-readable explanation of why this action was chosen.",
        },
    },
    "required": ["action", "confidence", "reasoning"],
}

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


class LLMAgentError(Exception):
    pass


def _build_failure_prompt(event_context: dict, rule_action: str, rule_confidence: float, rule_reasoning: str) -> str:
    return f"""A merchant payment/subscription event needs a recovery decision.

Event details:
- Event type: {event_context['event_type']}
- Failure reason: {event_context['failure_reason']}
- Attempt count so far: {event_context['attempt_count']}
- Amount: \u20b9{event_context['amount_inr']}
- Subscription ID: {event_context.get('subscription_id', 'n/a')}

Rules engine suggests:
- action: {rule_action}
- confidence: {rule_confidence}
- reasoning: {rule_reasoning}

Review this suggestion. You may confirm it or adjust confidence/reasoning.
You may NOT choose an action outside the fixed list in the tool schema.
Call {TOOL_NAME} with your final answer."""


def _build_dropoff_prompt(event_context: dict, rule_action: str, rule_confidence: float, rule_reasoning: str) -> str:
    return f"""A customer has abandoned checkout and needs a recovery decision.

Event details:
- Checkout status: {event_context['checkout_status']} (attempted = customer opened payment screen)
- Abandonment count (last 7 days, same subscription/customer): {event_context['abandonment_count']}
- Order amount: \u20b9{event_context['amount_inr']}
- Minutes since order created: {event_context['minutes_since_created']}
- Subscription ID: {event_context.get('subscription_id', 'n/a')}
- Incentive eligible: {event_context['incentive_eligible']}

Rules engine suggests:
- action: {rule_action}
- confidence: {rule_confidence}
- reasoning: {rule_reasoning}

Review this suggestion. You may confirm it or adjust confidence/reasoning.
You may NOT choose an action outside the fixed list in the tool schema.
Call {TOOL_NAME} with your final answer."""


def _call_groq(prompt: str) -> dict:
    if not settings.groq_api_key:
        raise LLMAgentError("No GROQ_API_KEY configured")

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
        "max_tokens": 400,
    }

    try:
        resp = httpx.post(
            GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=8.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMAgentError(f"Groq API call failed: {e}") from e

    data = resp.json()
    try:
        tool_call = data["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(tool_call["function"]["arguments"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMAgentError(f"Groq response missing usable tool call: {e}") from e

    if args.get("action") not in VALID_ACTIONS:
        raise LLMAgentError(f"Groq returned out-of-schema action: {args.get('action')}")
    return args


# ── Public entry points ──────────────────────────────────────────────────────

def decide(
    failure_reason: FailureReason,
    attempt_count: int,
    amount_inr: float,
    subscription_id: str | None = None,
    event_type: str = EventType.PAYMENT_FAILED,
) -> dict:
    """Payment failure decision path."""
    rule_action, rule_confidence, rule_reasoning = rule_based_decision(
        failure_reason, attempt_count, amount_inr
    )

    event_context = {
        "event_type": event_type,
        "failure_reason": failure_reason.value,
        "attempt_count": attempt_count,
        "amount_inr": amount_inr,
        "subscription_id": subscription_id,
    }

    return _run_llm(
        prompt_fn=_build_failure_prompt,
        event_context=event_context,
        rule_action=rule_action,
        rule_confidence=rule_confidence,
        rule_reasoning=rule_reasoning,
    )


def decide_dropoff(
    abandonment_count: int,
    amount_inr: float,
    checkout_status: str,
    incentive_eligible: bool,
    minutes_since_created: int,
    subscription_id: str | None = None,
) -> dict:
    """Checkout abandonment decision path."""
    rule_action, rule_confidence, rule_reasoning = rule_based_dropoff_decision(
        abandonment_count=abandonment_count,
        amount_inr=amount_inr,
        checkout_status=checkout_status,
        incentive_eligible=incentive_eligible,
    )

    event_context = {
        "checkout_status": checkout_status,
        "abandonment_count": abandonment_count,
        "amount_inr": amount_inr,
        "minutes_since_created": minutes_since_created,
        "subscription_id": subscription_id,
        "incentive_eligible": incentive_eligible,
    }

    return _run_llm(
        prompt_fn=_build_dropoff_prompt,
        event_context=event_context,
        rule_action=rule_action,
        rule_confidence=rule_confidence,
        rule_reasoning=rule_reasoning,
    )


def _run_llm(prompt_fn, event_context, rule_action, rule_confidence, rule_reasoning) -> dict:
    """Shared LLM call + graceful fallback logic used by both entry points."""
    if is_llm_failure_forced():
        raise_fallback = LLMAgentError("Simulated LLM failure (forced via /debug/toggle-llm-failure)")
        return _fallback(rule_action, rule_confidence, rule_reasoning, str(raise_fallback))

    prompt = prompt_fn(event_context, rule_action.value, rule_confidence, rule_reasoning)

    try:
        llm_result = _call_groq(prompt)
        return {
            "action": llm_result["action"],
            "confidence": float(llm_result["confidence"]),
            "reasoning": llm_result["reasoning"],
            "source": "llm_agent",
            "llm_error": None,
        }
    except LLMAgentError as e:
        return _fallback(rule_action, rule_confidence, rule_reasoning, str(e))


def _fallback(rule_action, rule_confidence, rule_reasoning, error_str) -> dict:
    return {
        "action": rule_action.value,
        "confidence": rule_confidence,
        "reasoning": f"[LLM unavailable, used rules-engine fallback] {rule_reasoning}",
        "source": "rules_engine_fallback",
        "llm_error": error_str,
    }

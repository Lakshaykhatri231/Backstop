"""
Executes whatever action the decision layer picked.
Handles both payment failure actions and drop-off actions.
Real external calls (Razorpay retry, email/SMS) are stubbed for demo
mode — the decision and audit trail are real regardless.
"""
import httpx

from app.config import settings
from app.models import AgentAction


def execute_action(action: str, event) -> tuple[bool, str]:
    """Returns (success, outcome_label)."""

    # ── Payment failure actions ──────────────────────────────────────────────
    if action == AgentAction.RETRY_NOW.value:
        return _retry_payment(event, delay_hours=0)

    if action == AgentAction.RETRY_LATER.value:
        return _retry_payment(event, delay_hours=24)

    if action == AgentAction.SEND_NUDGE.value:
        return _send_payment_nudge(event)

    # ── Drop-off / checkout abandonment actions ──────────────────────────────
    if action == AgentAction.SEND_REMINDER.value:
        return _send_reminder(event)

    if action == AgentAction.SEND_RESUME_LINK.value:
        return _send_resume_link(event)

    if action == AgentAction.OFFER_INCENTIVE.value:
        return _offer_incentive(event)

    if action == AgentAction.NO_ACTION.value:
        return True, "no_action_taken"

    # ── Shared ───────────────────────────────────────────────────────────────
    if action in (AgentAction.ESCALATE_TO_HUMAN.value, AgentAction.RULE_DEFAULT_FALLBACK.value):
        return _escalate(event)

    return False, "unknown_action"


# ── Payment failure handlers ─────────────────────────────────────────────────

def _retry_payment(event, delay_hours: int) -> tuple[bool, str]:
    if delay_hours > 0:
        return True, f"pending_retry_in_{delay_hours}h"

    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        return False, "no_razorpay_credentials_configured"

    try:
        # Real call would be:
        # resp = httpx.post(
        #     f"https://api.razorpay.com/v1/subscriptions/{event.subscription_id}/retry",
        #     auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        #     timeout=10.0,
        # )
        # resp.raise_for_status()
        # No real retry call is made yet - this must NOT claim "recovered".
        # Actual recovery is only ever confirmed by a subsequent
        # payment.captured webhook (see webhook.py's was_recovery check),
        # which is the sole source of truth for whether a retry worked.
        return True, "retry_scheduled"
    except httpx.HTTPError as e:
        return False, f"retry_failed:{e}"


def _send_payment_nudge(event) -> tuple[bool, str]:
    # Real: call email/SMS provider or Razorpay's hosted card-update link
    return True, "nudge_sent"


# ── Drop-off handlers ────────────────────────────────────────────────────────

def _send_reminder(event) -> tuple[bool, str]:
    # Real: send "you left something behind" email/SMS with order details
    return True, "reminder_sent"


def _send_resume_link(event) -> tuple[bool, str]:
    # Real: generate a one-click checkout resume URL (Razorpay payment link
    # pointing at the same order_id) and deliver it to the customer
    return True, "resume_link_sent"


def _offer_incentive(event) -> tuple[bool, str]:
    # Real: generate a coupon for the event's incentive_pct% off, attach it
    # to a resume link, deliver to customer.
    #
    # The % is read off the event, not from config: there is no single flat
    # discount any more. Each tier has a band, and a customer's exact
    # position inside it comes from their engagement score
    # (tiering.incentive_pct_for_customer), snapshotted onto the CartEvent
    # at decision time so the terms shown can never drift if config changes
    # later. Still fixed by policy and still fully deterministic - the LLM
    # never chose it, which is why this action is safe to auto-execute once
    # all three incentive gates pass.
    pct = getattr(event, "incentive_pct", None)
    if pct is None:
        # Reached only if an action/decision mismatch let offer_incentive
        # through without a sized offer. Fail loudly in the outcome label
        # rather than silently claiming a 0% coupon was issued.
        return False, "incentive_offer_failed_no_pct"
    return True, f"incentive_offered_{round(pct)}pct"


# ── Shared ────────────────────────────────────────────────────────────────────

def _escalate(event) -> tuple[bool, str]:
    return True, "escalated"

"""
Rules engine: deterministic first pass at a decision.

Two entry points:
- rule_based_decision()        → for payment failure events
- rule_based_dropoff_decision() → for checkout abandonment events

The LLM layer sits on top of both, but every action it can pick is still
constrained to the same AgentAction enum defined here.
"""
from app.models import FailureReason, AgentAction, CustomerTier, CartEventType
from app.config import settings
from app import runtime_flags


# ── Payment failure decisions ────────────────────────────────────────────────

def rule_based_decision(
    failure_reason: FailureReason,
    attempt_count: int,
    amount_inr: float,
) -> tuple[AgentAction, float, str]:
    """Returns (action, confidence, reasoning) for a payment failure event.

    Ladder, roughly in order of how much confidence we have that a retry
    will actually help:
    - risk_block: never auto-retry, always human review - retrying a
      fraud-flagged payment is unsafe, not just unhelpful.
    - card_expired, cancelled: retrying literally cannot succeed (expired
      card) or would be pushing right after an explicit "no" (cancelled) -
      message-only, no retry button, ever.
    - unknown, invalid_card: one honest retry, since we're not claiming to
      know the cause - but if it fails again, two unclassifiable failures
      in a row is a stronger signal than one, so escalate immediately
      rather than burning the full 3-attempt runway on something we can't
      explain.
    - insufficient_funds: retrying instantly won't help since the money
      genuinely isn't there yet - retry_later from the first attempt.
    - bank_decline, network_error, authentication_failed: genuinely
      transient-looking failures - retry_now on attempt 1, back off to
      retry_later on attempts 2-3, hard escalate at attempt 4 (below).
    """

    if attempt_count > runtime_flags.get_max_auto_retries():
        return (
            AgentAction.ESCALATE_TO_HUMAN,
            0.95,
            f"Attempt count ({attempt_count}) exceeds max auto-retries "
            f"({runtime_flags.get_max_auto_retries()}); handing off to human review.",
        )

    if amount_inr >= runtime_flags.get_high_value_amount_inr():
        return (
            AgentAction.ESCALATE_TO_HUMAN,
            0.60,
            f"Amount \u20b9{amount_inr:.0f} is above the high-value threshold "
            f"(\u20b9{runtime_flags.get_high_value_amount_inr()}); routing to human approval.",
        )

    if failure_reason == FailureReason.RISK_BLOCK:
        return (
            AgentAction.ESCALATE_TO_HUMAN,
            0.85,
            "Risk/fraud block on the transaction. Auto-retrying a risk-flagged payment "
            "is unsafe; requires human review before any further attempt.",
        )

    if failure_reason == FailureReason.CARD_EXPIRED:
        return (
            AgentAction.SEND_NUDGE,
            0.90,
            "Card has expired - retrying will not help. Customer needs to update "
            "payment method before any charge can succeed.",
        )

    if failure_reason == FailureReason.CANCELLED:
        return (
            AgentAction.SEND_NUDGE,
            0.70,
            "Customer explicitly cancelled at the payment screen - this is a "
            "deliberate action, not a decline. Pushing an immediate retry right "
            "after an explicit no risks burning goodwill; no retry button offered.",
        )

    if failure_reason in (FailureReason.UNKNOWN, FailureReason.INVALID_CARD):
        if attempt_count == 1:
            return (
                AgentAction.RETRY_NOW,
                0.55 if failure_reason == FailureReason.UNKNOWN else 0.60,
                "Unclassifiable failure - not claiming to know the cause, but a "
                "single retry is low-cost enough to be worth one honest attempt "
                "before involving a human."
                if failure_reason == FailureReason.UNKNOWN else
                "Card was rejected outright (not a bank-side decline) - most likely "
                "a wrong number or unsupported card type. Offering one retry with "
                "guidance to use a different card.",
            )
        return (
            AgentAction.ESCALATE_TO_HUMAN,
            0.75,
            f"Second consecutive unclassifiable failure ({failure_reason.value}). "
            "Two unexplained failures in a row is a stronger signal than one - "
            "escalating now rather than burning the full retry budget on "
            "something we can't explain.",
        )

    if failure_reason == FailureReason.INSUFFICIENT_FUNDS:
        return (
            AgentAction.RETRY_LATER,
            0.80 if attempt_count == 1 else 0.55,
            "First insufficient-funds decline. Often transient (salary/payday timing). "
            "Retrying in 24h gives the account time to be funded."
            if attempt_count == 1 else
            f"Repeated insufficient-funds decline (attempt {attempt_count}). Lower "
            "confidence this resolves on its own; still worth one more delayed "
            "retry before escalating.",
        )

    if failure_reason in (FailureReason.BANK_DECLINE, FailureReason.NETWORK_ERROR, FailureReason.AUTHENTICATION_FAILED):
        if attempt_count == 1:
            reason_text = {
                FailureReason.BANK_DECLINE: "Generic bank decline, no clear cause.",
                FailureReason.NETWORK_ERROR: "Network/gateway-level error, not a customer-side decline.",
                FailureReason.AUTHENTICATION_FAILED: "OTP/3DS authentication failed - often just a mistyped code.",
            }[failure_reason]
            return (
                AgentAction.RETRY_NOW,
                0.75 if failure_reason == FailureReason.NETWORK_ERROR else 0.65,
                f"{reason_text} Immediate retry is worth attempting in case it "
                "was a transient hiccup.",
            )
        return (
            AgentAction.RETRY_LATER,
            0.50,
            f"Repeated {failure_reason.value} (attempt {attempt_count}). An immediate "
            "retry already didn't help - backing off rather than pushing again "
            "right away.",
        )

    return (
        AgentAction.ESCALATE_TO_HUMAN,
        0.30,
        "Failure reason code not recognized by the rules engine. Escalating rather "
        "than guessing at an action.",
    )


# ── Drop-off / checkout abandonment decisions ────────────────────────────────

def rule_based_dropoff_decision(
    abandonment_count: int,
    amount_inr: float,
    checkout_status: str,
    incentive_eligible: bool,
) -> tuple[AgentAction, float, str]:
    """
    Returns (action, confidence, reasoning) for a checkout abandonment event.

    abandonment_count  : how many times this subscription/customer has abandoned
                         checkout within the lookback window (7 days).
    amount_inr         : order value.
    checkout_status    : "created" | "attempted" — only "attempted" should reach
                         here (poller filters out "created"), but we handle both.
    incentive_eligible : True if the three incentive gates all pass (amount cap,
                         frequency cap, no prior escalation). Checked in dropoff.py
                         before calling here.
    """

    # Sanity: if someone never actually opened the checkout, don't nudge them.
    if checkout_status == "created":
        return (
            AgentAction.NO_ACTION,
            0.90,
            "Order status is 'created' — checkout was never opened by the customer. "
            "No recovery action warranted; logging for metric tracking only.",
        )

    # High-value orders always go to human regardless of abandonment count.
    if amount_inr >= runtime_flags.get_high_value_amount_inr():
        return (
            AgentAction.ESCALATE_TO_HUMAN,
            0.85,
            f"High-value order (\u20b9{amount_inr:.0f} >= \u20b9{runtime_flags.get_high_value_amount_inr()}). "
            "Routing to human review regardless of abandonment count.",
        )

    # 1st abandonment — soft reminder, no money involved yet.
    if abandonment_count <= 1:
        return (
            AgentAction.SEND_REMINDER,
            0.85,
            f"First checkout abandonment for this subscription/customer "
            f"(\u20b9{amount_inr:.0f} order, status: {checkout_status}). "
            "Sending a soft reminder to reduce friction.",
        )

    # 2nd abandonment — stronger action; offer incentive if gates pass.
    if abandonment_count == 2:
        if incentive_eligible:
            return (
                AgentAction.OFFER_INCENTIVE,
                0.80,
                f"Second abandonment (\u20b9{amount_inr:.0f}). Customer engaged checkout "
                "but didn't convert twice. Offering an incentive to close the gap - "
                "eligibility gates passed. The exact discount is sized from the "
                "customer's tier band and engagement score at execution time, not "
                "fixed here (see tiering.incentive_pct_for_customer).",
            )
        return (
            AgentAction.SEND_RESUME_LINK,
            0.75,
            f"Second abandonment (\u20b9{amount_inr:.0f}). Incentive gate did not pass "
            "(order value, frequency cap, or prior escalation). "
            "Sending a one-click resume link to reduce friction instead.",
        )

    # 3rd+ abandonment — automated nudges aren't working; escalate.
    return (
        AgentAction.ESCALATE_TO_HUMAN,
        0.92,
        f"Third or more abandonment (count: {abandonment_count}, "
        f"\u20b9{amount_inr:.0f}). Repeated automated recovery attempts "
        "haven't converted this customer. Handing off for manual review.",
    )


# ── Pre-checkout cart events (silent abandon vs explicit cancel) ────────────

def rule_based_cart_event_decision(
    event_type: CartEventType,
    tier: CustomerTier,
    amount_inr: float,
    repeat_cancel_count: int,
    amount_cap_ok: bool,
    freq_cap_ok: bool,
) -> tuple[AgentAction, float, str]:
    """
    Returns (action, confidence, reasoning) for a pre-checkout cart event -
    i.e. no Razorpay order exists yet at all (see CartEvent model).

    The key design choice: silent_abandon and explicit_cancel are treated as
    different *strength of signal*, not just different labels. A silent
    abandon is ambiguous (could be distraction, could come back on its own).
    An explicit cancel is the customer actively saying no - chasing that
    immediately with a hard sell risks burning goodwill, so it's handled
    more conservatively across every tier, not just escalated faster.

    amount_cap_ok / freq_cap_ok: the two incentive money-gates, checked
    separately by the caller and passed in as distinct flags rather than
    folded into one boolean - specifically so each can be told apart in
    CartEvent/the audit trail (previously only the frequency cap was even
    captured, under the misleading name "incentive_eligible"). Without the
    frequency cap, a high-tier customer could farm a discount every single
    time they hit the timeout/cancel buttons - the tier label alone never
    re-checks how many times this customer has already been paid to come
    back.

    A third gate - tier eligibility - is resolved here via
    settings.incentive_eligible_tiers. NEW and RISK are hardcoded out of
    that set below and can never become incentive-eligible no matter what
    config says: that's a deliberate anti-abuse / low-signal guardrail,
    not a tunable economics parameter (see config.py).
    """
    tier_incentive_eligible = tier in (
        CustomerTier.CASUAL, CustomerTier.REGULAR, CustomerTier.LOYAL,
    ) and (
        tier.value in runtime_flags.get_incentive_eligible_tiers()
    )
    incentive_ok = tier_incentive_eligible and amount_cap_ok and freq_cap_ok

    # A repeatedly-cancelling customer is a distinct risk signal regardless
    # of their tier's origin story - don't keep spending recovery effort on it.
    # Note the caller windows this count (it used to be all-time, which meant
    # three cancels once left a customer escalated forever with no way back).
    if event_type == CartEventType.EXPLICIT_CANCEL and repeat_cancel_count >= 3:
        return (
            AgentAction.ESCALATE_TO_HUMAN,
            0.88,
            f"Customer has explicitly cancelled {repeat_cancel_count} times recently. "
            "Automated win-back is unlikely to help further; flagging for human review "
            "rather than continuing to spend recovery effort.",
        )

    if tier == CustomerTier.RISK:
        if event_type == CartEventType.EXPLICIT_CANCEL:
            return (
                AgentAction.NO_ACTION,
                0.85,
                "Risk-tier customer explicitly cancelled. Not worth spending recovery "
                "effort on a customer with a poor payment/cancellation history who has "
                "also just declined outright.",
            )
        return (
            AgentAction.SEND_REMINDER,
            0.70,
            "Risk-tier customer, but only a silent (ambiguous) abandonment - one "
            "low-cost reminder is sent, no incentive, given the weaker recovery odds.",
        )

    if tier == CustomerTier.NEW:
        if event_type == CartEventType.EXPLICIT_CANCEL:
            return (
                AgentAction.NO_ACTION,
                0.75,
                "New customer with no purchase history explicitly cancelled. Too little "
                "signal to justify a targeted win-back; a generic follow-up (if any) "
                "should come from marketing, not the recovery agent.",
            )
        return (
            AgentAction.SEND_REMINDER,
            0.80,
            "New customer, silent cart abandonment. Sending one reminder - no "
            "incentive yet, since there's no history to justify the spend.",
        )

    if tier == CustomerTier.LOYAL:
        if event_type == CartEventType.EXPLICIT_CANCEL:
            if incentive_ok:
                return (
                    AgentAction.OFFER_INCENTIVE,
                    0.68,
                    "Loyal customer explicitly cancelled, but has the strongest "
                    "conversion odds in the system and hasn't been incentivized "
                    "recently. Leading with a discount to win back a likely-genuine "
                    "hesitation rather than just a soft resume link. This tier gets "
                    "the largest discount band but the tightest frequency cap (1 per "
                    "30 days), which is what bounds how often this can fire.",
                )
            return (
                AgentAction.SEND_RESUME_LINK,
                0.65,
                "Loyal customer explicitly cancelled, but is not currently "
                "incentive-eligible (tier gate, order-value cap, or 30-day "
                "frequency cap). Falling back to a soft, no-discount resume "
                "link instead.",
            )
        if incentive_ok:
            return (
                AgentAction.OFFER_INCENTIVE,
                0.82,
                "Loyal customer, silent cart abandonment. High conversion odds and "
                "proven value justify leading with an incentive immediately "
                "(within the order-value cap and frequency cap).",
            )
        return (
            AgentAction.SEND_REMINDER,
            0.75,
            "Loyal customer, silent cart abandonment, but not currently "
            "incentive-eligible (tier gate, order-value cap, or 30-day "
            "frequency cap). Sending a plain reminder instead of a discount.",
        )

    if tier == CustomerTier.REGULAR:
        if event_type == CartEventType.EXPLICIT_CANCEL:
            if incentive_ok:
                return (
                    AgentAction.OFFER_INCENTIVE,
                    0.65,
                    "Regular customer explicitly cancelled. Consistent purchase "
                    "history justifies a mid-band discount to recover the cart, "
                    "sized from their engagement score rather than a flat rate.",
                )
            return (
                AgentAction.SEND_RESUME_LINK,
                0.62,
                "Regular customer explicitly cancelled, not currently "
                "incentive-eligible (tier gate, order-value cap, or 30-day "
                "frequency cap). Soft resume link instead of a discount.",
            )
        if incentive_ok:
            return (
                AgentAction.OFFER_INCENTIVE,
                0.77,
                "Regular customer, silent cart abandonment. Established buying "
                "pattern and an ambiguous (not explicitly refused) signal - a "
                "mid-band incentive is the highest-expected-value action.",
            )
        return (
            AgentAction.SEND_REMINDER,
            0.76,
            "Regular customer, silent cart abandonment, not currently "
            "incentive-eligible (tier gate, order-value cap, or 30-day "
            "frequency cap). Plain reminder instead.",
        )

    # CASUAL - the ladder's entry rung, for anyone with real purchase
    # history who hasn't yet earned Regular. Incentive-capable by default
    # now, unlike the tier it replaces: its discount band tops out at 10%,
    # so opening it up no longer means paying a top-tier rate to a
    # bottom-tier customer, which was the reason to keep it out before.
    if event_type == CartEventType.EXPLICIT_CANCEL:
        if incentive_ok:
            return (
                AgentAction.OFFER_INCENTIVE,
                0.62,
                "Casual customer explicitly cancelled. Small, bounded discount "
                "from the entry-tier band (order-value and frequency caps both "
                "pass) - cheap enough per offer to be worth trying on a "
                "lower-signal customer.",
            )
        return (
            AgentAction.SEND_RESUME_LINK,
            0.60,
            "Casual customer explicitly cancelled. Not currently "
            "incentive-eligible (tier gate, order-value cap, or 30-day "
            "frequency cap) - sending a low-key 'saved for later' link "
            "instead, respecting the explicit no while leaving the door open.",
        )
    if incentive_ok:
        return (
            AgentAction.OFFER_INCENTIVE,
            0.72,
            "Casual customer, silent cart abandonment. Entry-tier discount "
            "band, with the order-value and frequency caps both passing.",
        )
    return (
        AgentAction.SEND_REMINDER,
        0.78,
        "Casual customer, silent cart abandonment - ambiguous signal "
        "and not currently incentive-eligible (tier gate, order-value cap, "
        "or 30-day frequency cap), so a straightforward reminder is sent.",
    )

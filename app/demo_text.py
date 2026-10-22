"""
Plain-English wording for the Demo Console timeline.

The system's own labels (`mandate_retry · cancelled — cancelled:promise_opened`,
`voice_call_placed:promise_created:2026-10-24`) are precise but read like log
lines. This module turns the same facts into sentences a non-engineer can
follow. It only RE-WORDS what is already recorded: it never decides anything,
and unknown codes fall back to a neutral sentence (the raw code is returned
separately as `raw`, shown on hover).
"""
from datetime import datetime, timedelta

_IST = timedelta(hours=5, minutes=30)

KIND_PLAIN = {
    "mandate_retry": "collect the payment again",
    "pre_debit_notice": "tell the customer about the retry",
    "promise_reminder": "remind the customer of their promised date",
    "promise_check": "check whether the promise was kept",
    "invoice_chase": "chase the invoice",
    "voice_call": "call the customer",
    "mandate_pdn": "set up the next autopay debit with Razorpay",
    "mandate_debit": "debit the customer's card",
    "order_link_reminder": "remind the shopper about their payment link",
}

BLOCK_PLAIN = {
    "phone_not_verified": "the customer has not verified their number yet",
    "no_voice_consent": "the customer has not agreed to calls",
    "no_phone_number": "there is no phone number on file",
    "outside_contact_window": "it is outside the 08:00-19:00 calling hours",
    "one_call_per_day": "a call was already placed today",
    "max_attempts_reached": "the maximum number of calls was reached",
    "promise_open": "the customer has promised a payment date",
    "wrong_number_flagged": "this number was reported as a wrong number",
    "dnd_registered": "the number is on the Do-Not-Disturb list",
    "run_not_active": "this debt is no longer being chased",
    "retry_already_due": "the retry is already due, so a call would be pointless",
    "invoice_closed": "the invoice is already closed",
    "invoice_disputed": "the invoice is disputed",
    "payment_resolved": "the payment is already resolved",
    "below_min_order_value": "the amount is below the minimum for calls",
    "unknown_subject": "the debt no longer exists",
    "voice_provider_not_configured": "no voice provider is configured",
}

CANCEL_PLAIN = {
    "promise_opened": "the customer made a promise, so chasing pauses until that date",
    "promise_broken": "the promise was broken, so automatic steps stopped",
    "superseded_by_new_failure": "a newer failed payment replaced this plan",
    "invoice_disputed": "the customer disputed the invoice, so automatic chasing stopped",
    "unauthorised_debit_claim": "the customer says they never authorised the debit, so all automatic attempts stopped",
}


def _ist(dt: datetime | None, with_time: bool = True) -> str:
    if dt is None:
        return ""
    return (dt + _IST).strftime("%d %b, %H:%M" if with_time else "%d %b")


def _blk(code: str) -> str:
    return BLOCK_PLAIN.get(code, code.replace("_", " "))


def outcome_text(routed: str | None) -> tuple[str, str]:
    """(sentence, tone) for what a call (or call-like step) ended up doing."""
    r = routed or ""
    if r.startswith("voice_call_placed:"):
        r = r[len("voice_call_placed:"):]
    if r.startswith("promise_created:"):
        d = r.split(":", 1)[1]
        try:
            d = datetime.strptime(d, "%Y-%m-%d").strftime("%d %b")
        except ValueError:
            pass
        return f"They promised to pay on {d}. The promise is recorded and chasing pauses until then.", "ok"
    if r == "payment_link_sent":
        return "They said they would pay now, so a payment link was texted to them.", "ok"
    if r.startswith("payment_link_not_sent"):
        return "They said they would pay, but the payment link could not be sent.", "warn"
    if r.startswith("payment_link_resent"):
        return "They promised to pay; the payment link was sent again.", "ok"
    if r.startswith("escalated:promise_rejected"):
        why = r.split(":")[-1]
        if why == "date_too_far":
            return "They named a date that is too far away to accept, so a person will follow up.", "warn"
        if why == "no_date":
            return ("They promised to pay but no date could be read from what they said, so a "
                    "person will follow up. Try a clear date such as \"in 3 days\"."), "warn"
        return "They named a date that could not be accepted, so a person will follow up.", "warn"
    if r == "escalated:unauthorised_debit_claim":
        return ("They say they never authorised this debit. All automatic attempts stopped "
                "and a person must review it."), "bad"
    if r in ("escalated:payment_dispute", "invoice_disputed"):
        return "They dispute the amount. Automatic chasing stopped and a person will decide.", "bad"
    if r == "voice_stopped_wrong_number":
        return "Wrong number. Calls to this number are stopped.", "warn"
    if r == "callback_scheduled":
        return "They asked to be called later. A callback is scheduled for tomorrow morning.", "info"
    if r.startswith("escalated:refused"):
        return "They refused to pay, so a person will follow up.", "warn"
    if r.startswith("escalated:unclear"):
        return "The answer was unclear, so a person will follow up.", "warn"
    if r.startswith("escalated:max_attempts") or r.endswith("max_attempts_reached:escalated"):
        return "All call attempts were used, so it goes to a person.", "warn"
    if r.startswith("voice_blocked:"):
        return f"The call did not go ahead: {_blk(r.split(':')[1])}.", "warn"
    return "The call was handled.", "info"


def describe_job(kind: str, status: str, result: str | None, due_at: datetime | None,
                 executed_at: datetime | None) -> dict:
    res = result or ""
    what = KIND_PLAIN.get(kind, kind.replace("_", " "))
    at = executed_at or due_at

    if status == "scheduled":
        return {"title": f"Planned: {what}", "detail": f"Due {_ist(due_at)} IST.", "tone": "info", "at": due_at}

    if status == "cancelled":
        reason = res.split(":", 1)[1] if ":" in res else res
        if reason.startswith("run_settled"):
            why = "the debt was paid, so no further steps are needed"
        elif reason.startswith("invoice_"):
            why = CANCEL_PLAIN.get(reason, "the invoice is closed")
        elif reason.startswith("promise_") and reason not in CANCEL_PLAIN:
            why = "the promise was settled, so its reminders are no longer needed"
        else:
            why = CANCEL_PLAIN.get(reason, reason.replace("_", " "))
        return {"title": f"Cancelled: {what}", "detail": f"Because {why}.", "tone": "info", "at": at}

    if status == "skipped":
        if res.startswith("voice_blocked:"):
            return {"title": "Call skipped", "detail": f"Because {_blk(res.split(':')[1])}.",
                    "tone": "warn", "at": at}
        if res.startswith("paused:"):
            code = res.split(":", 1)[1]
            why = {"promise_open": "the customer has promised a date",
                   "disputed": "the invoice is disputed", "paid": "nothing is owed any more"}.get(code, code)
            return {"title": "Chase step paused", "detail": f"Because {why}.", "tone": "info", "at": at}
        why = {"run_not_active": "this debt is no longer being chased",
               "promise_not_open": "the promise was already settled",
               "invoice_closed": "the invoice is already closed"}.get(res)
        if why:
            return {"title": f"Skipped: {what}", "detail": f"Because {why}.", "tone": "info", "at": at}
        return {"title": f"Could not run: {what}", "detail": "Something went wrong running this step.",
                "tone": "bad", "at": at}

    # done
    if kind == "mandate_retry":
        if res == "no_razorpay_credentials_configured":
            return {"title": "Tried to collect the payment again",
                    "detail": "Nothing was charged: this demo has no Razorpay keys, so the bank never gets asked.",
                    "tone": "info", "at": at}
        return {"title": "Tried to collect the payment again",
                "detail": "Waiting for the bank's answer.", "tone": "info", "at": at}
    if kind == "pre_debit_notice":
        if res.startswith("pre_debit_notice_sent"):
            return {"title": "Told the customer the payment will be retried",
                    "detail": "A text gave the new date and a link to choose a different one.",
                    "tone": "ok", "at": at}
        why = "there is no phone number on file" if "no_contact" in res else "the message could not be sent"
        return {"title": "Could not tell the customer about the retry", "detail": f"Because {why}.",
                "tone": "bad", "at": at}
    if kind == "promise_reminder":
        if res == "reminder_sent":
            return {"title": "Reminded the customer of the date they promised", "detail": "", "tone": "ok", "at": at}
        return {"title": "Could not remind the customer", "detail": "The reminder text was not sent.",
                "tone": "warn", "at": at}
    if kind == "promise_check":
        return {"title": "The promised date passed without payment",
                "detail": "The promise is marked broken and the debt goes to a person.",
                "tone": "bad", "at": at}
    if kind == "voice_call":
        if res == "voice_call_ringing":
            return {"title": "Called the customer", "detail": "The phone is ringing: answer it in the pop-up.",
                    "tone": "info", "at": at}
        text, tone = outcome_text(res)
        return {"title": "Called the customer", "detail": text, "tone": tone, "at": at}
    if kind == "invoice_chase":
        if res == "reminder_sent":
            return {"title": "Sent a friendly reminder before the due date", "detail": "", "tone": "ok", "at": at}
        if res == "payment_link_sent":
            return {"title": "Sent the customer a payment link", "detail": "", "tone": "ok", "at": at}
        if res.startswith("voice_skipped("):
            why = res[len("voice_skipped("):].split(")")[0]
            code = why.split(":")[-1]
            return {"title": "Wanted to call, sent a payment link instead",
                    "detail": f"The call could not go ahead because {_blk(code)}.", "tone": "info", "at": at}
        if res == "escalated":
            return {"title": "Handed to the finance team",
                    "detail": "The whole reminder ladder was used with no payment.", "tone": "warn", "at": at}
        if "not_sent" in res:
            return {"title": "Could not send the reminder", "detail": "No phone number or the message failed.",
                    "tone": "bad", "at": at}
        text, tone = outcome_text(res)
        return {"title": "Chased the invoice", "detail": text, "tone": tone, "at": at}
    return {"title": f"Did: {what}", "detail": "", "tone": "info", "at": at}


def describe_call(status: str, routed_to: str | None, transcript: str | None) -> dict:
    if status == "in_progress":
        return {"title": "Call is ringing", "detail": "Waiting for the customer to answer.", "tone": "info"}
    if status == "no_answer":
        text, tone = outcome_text(routed_to)
        return {"title": "The customer did not answer", "detail": text, "tone": "info"}
    text, tone = outcome_text(routed_to)
    return {"title": "Spoke with the customer", "detail": text, "tone": tone}


def describe_message(template: str, label: str, status: str) -> dict:
    detail = {
        "recorded": "Delivered to the customer's inbox (simulated: no real SMS is sent).",
        "sent": "Handed to the SMS provider.",
        "failed": "Could not be sent.",
        "not_sent_no_contact": "Not sent: there is no phone number on file.",
    }.get(status, status)
    tone = "ok" if status in ("recorded", "sent") else "bad"
    return {"title": f"Text message: {label}", "detail": detail, "tone": tone}


def describe_promise(promised_date, status: str) -> dict:
    d = promised_date.strftime("%d %b")
    detail, tone = {
        "open": ("Waiting for that date. Chasing is paused.", "info"),
        "kept": ("Kept: the payment arrived.", "ok"),
        "partially_kept": ("Partly kept: only some of it was paid.", "warn"),
        "broken": ("Broken: no payment by the date.", "bad"),
        "cancelled": ("Cancelled.", "info"),
    }.get(status, (status, "info"))
    return {"title": f"The customer promised to pay on {d}", "detail": detail, "tone": tone}

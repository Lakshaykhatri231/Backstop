"""
Outbound customer text messages - the one place a notice, payment link,
re-mandate link or reminder leaves the system.

Before this module those were stubs in actions.py that returned a label and
sent nothing, so nobody could SEE what a customer would receive. Now every
message is rendered from a fixed template (the LLM never writes it - amounts,
dates and links are slotted in), stored in `outbound_messages`, and handed to
a provider:

- simulated (default): stored only. The Demo Console inbox reads this table,
  so "what did the customer get, and when" is visible and checkable.
- msg91: sent through MSG91's Flow API using a DLT-approved template id per
  message kind. A kind with no template id is recorded as FAILED - never
  sent as free text (DLT forbids it) and never silently dropped.

A message with nobody to send to is recorded as `not_sent_no_contact`: the
notice was due, the system had no number. Callers must report that honestly
rather than claim "sent".

Transactional only: pre-debit notice, payment links, reminders about a debt
the customer already has. No marketing text goes through here.
"""
import logging
import re
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app import clock
from app.audit import write_audit_entry
from app.config import settings
from app.models import OutboundMessage

logger = logging.getLogger(__name__)

MERCHANT_NAME = "Backstop Store"
LANGUAGES = ("hinglish", "hi", "en")

# Variable order is the contract with the DLT template: MSG91 receives them as
# var1..varN in exactly this order, so a template must declare them the same way.
TEMPLATE_VARS: dict[str, list[str]] = {
    "pre_debit_notice": ["name", "merchant", "amount", "date", "link"],
    "payment_link": ["name", "merchant", "amount", "link"],
    "remandate_link": ["name", "merchant", "amount", "link"],
    "promise_reminder": ["name", "merchant", "amount", "date", "link"],
    "invoice_courtesy": ["name", "merchant", "ref", "amount", "due", "link"],
    "invoice_overdue": ["name", "merchant", "ref", "amount", "due", "link"],
    # Failed storefront order whose cart ALREADY had an offer (app/order_links.py).
    # `original` is pre-formatted ("₹1,000"); the link expires with the offer.
    "payment_link_offer": ["name", "merchant", "pct", "original", "amount", "until", "link"],
    "order_offer_ended": ["name", "merchant", "pct", "until", "link"],
}

_BODIES: dict[tuple[str, str], str] = {
    ("pre_debit_notice", "en"):
        "Hi {name}, {merchant}: your debit of {amount} did not go through. We will try again on "
        "{date}. Please keep your account funded. Choose a date or details here: {link}",
    ("pre_debit_notice", "hinglish"):
        "Namaste {name} ji, {merchant}: aapka {amount} ka debit fail ho gaya tha. Hum {date} ko "
        "dobara try karenge. Kripya account mein balance rakhein. Date chunne ke liye: {link}",
    ("pre_debit_notice", "hi"):
        "नमस्ते {name} जी, {merchant}: आपका {amount} का डेबिट विफल हो गया था। हम {date} को दोबारा "
        "प्रयास करेंगे। कृपया खाते में बैलेंस रखें। तारीख चुनें: {link}",
    ("payment_link", "en"):
        "Hi {name}, {merchant}: pay your pending {amount} here: {link}",
    ("payment_link", "hinglish"):
        "Namaste {name} ji, {merchant}: aapka pending {amount} yahan pay karein: {link}",
    ("payment_link", "hi"):
        "नमस्ते {name} जी, {merchant}: आपका बकाया {amount} यहाँ चुकाएँ: {link}",
    ("remandate_link", "en"):
        "Hi {name}, {merchant}: your autopay mandate has stopped. Set it up again to continue "
        "your {amount} payment: {link}",
    ("remandate_link", "hinglish"):
        "Namaste {name} ji, {merchant}: aapka autopay mandate band ho gaya hai. {amount} ka "
        "payment jaari rakhne ke liye dobara set karein: {link}",
    ("remandate_link", "hi"):
        "नमस्ते {name} जी, {merchant}: आपका ऑटोपे मैंडेट बंद हो गया है। {amount} का भुगतान जारी "
        "रखने के लिए दोबारा सेट करें: {link}",
    ("promise_reminder", "en"):
        "Hi {name}, {merchant}: a reminder that you planned to pay {amount} on {date}. Pay here: {link}",
    ("promise_reminder", "hinglish"):
        "Namaste {name} ji, {merchant}: yaad dilana tha, aapne {date} ko {amount} pay karne ka "
        "bola tha. Yahan pay karein: {link}",
    ("promise_reminder", "hi"):
        "नमस्ते {name} जी, {merchant}: याद दिलाना था, आपने {date} को {amount} चुकाने को कहा था। "
        "यहाँ चुकाएँ: {link}",
    ("invoice_courtesy", "en"):
        "Hello {name}, {merchant}: invoice {ref} for {amount} is due on {due}. Pay online: {link}",
    ("invoice_courtesy", "hinglish"):
        "Namaste {name} ji, {merchant}: invoice {ref}, {amount}, {due} ko due hai. Online pay "
        "karein: {link}",
    ("invoice_courtesy", "hi"):
        "नमस्ते {name} जी, {merchant}: इनवॉइस {ref}, {amount}, {due} को देय है। ऑनलाइन चुकाएँ: {link}",
    ("payment_link_offer", "en"):
        "Hi {name}, {merchant}: your {pct}% off is still saved. Pay {amount} instead of {original}, "
        "valid till {until}: {link}",
    ("payment_link_offer", "hinglish"):
        "Namaste {name} ji, {merchant}: aapka {pct}% off abhi bhi saved hai. {original} ki jagah "
        "sirf {amount} pay karein, {until} tak: {link}",
    ("payment_link_offer", "hi"):
        "नमस्ते {name} जी, {merchant}: आपकी {pct}% छूट अभी भी सुरक्षित है। {original} की जगह सिर्फ़ "
        "{amount} चुकाएँ, {until} तक: {link}",
    ("order_offer_ended", "en"):
        "Hi {name}, {merchant}: your {pct}% offer ended on {until}. Your items are still in your "
        "cart: {link}",
    ("order_offer_ended", "hinglish"):
        "Namaste {name} ji, {merchant}: aapka {pct}% offer {until} ko khatam ho gaya. Aapke items "
        "abhi bhi cart mein hain: {link}",
    ("order_offer_ended", "hi"):
        "नमस्ते {name} जी, {merchant}: आपका {pct}% ऑफ़र {until} को समाप्त हो गया। आपके सामान अभी भी "
        "कार्ट में हैं: {link}",
    ("invoice_overdue", "en"):
        "Hello {name}, {merchant}: invoice {ref} for {amount} was due on {due} and is unpaid. "
        "Please pay now: {link}",
    ("invoice_overdue", "hinglish"):
        "Namaste {name} ji, {merchant}: invoice {ref}, {amount}, {due} ko due tha aur abhi pending "
        "hai. Kripya abhi pay karein: {link}",
    ("invoice_overdue", "hi"):
        "नमस्ते {name} जी, {merchant}: इनवॉइस {ref}, {amount}, {due} को देय था और अभी बकाया है। "
        "कृपया अभी चुकाएँ: {link}",
}

_OTP_BODY = {
    "en": "{code} is your verification code. It expires in {mins} minutes. Do not share it.",
    "hinglish": "{code} aapka verification code hai. {mins} minute mein expire hoga. Kisi ko na batayein.",
    "hi": "{code} आपका वेरिफिकेशन कोड है। यह {mins} मिनट में समाप्त होगा। किसी को न बताएँ।",
}


def _inr(amount: float) -> str:
    return f"₹{amount:,.0f}"


def absolute_link(link: str | None) -> str | None:
    if link and link.startswith("/"):
        return settings.public_base_url.rstrip("/") + link
    return link


_ANON_NAMES = ("", "customer")


def drop_name(template_text: str) -> str:
    """Greeting without a name ("Namaste, ..." not "Namaste Customer ji, ...")
    for debts where we genuinely do not know the person's name (a Razorpay
    customer id carries none)."""
    return re.sub(r"\s*\{name\}(?: ji| जी)?", "", template_text)


def render(template: str, language: str, variables: dict) -> str:
    lang = language if language in LANGUAGES else "hinglish"
    body = _BODIES.get((template, lang)) or _BODIES[(template, "hinglish")]
    if str(variables.get("name", "")).strip().lower() in _ANON_NAMES:
        body = drop_name(body)
    vals = {"merchant": MERCHANT_NAME, "name": "Customer", "link": "", "date": "", "due": "",
            "ref": "", "pct": "", "original": "", "until": "", **variables}
    if isinstance(vals.get("amount"), (int, float)):
        vals["amount"] = _inr(float(vals["amount"]))
    text = body.format(**vals)
    if not str(vals.get("link") or "").strip():
        # No link to give (a courtesy reminder never spends a Test Mode link): don't end on "Pay online:" with
        # nothing after it. Cut back to the end of the last full sentence.
        trimmed = text.rstrip()
        if trimmed.endswith(":"):
            cut = max(trimmed.rfind(". "), trimmed.rfind("। "), trimmed.rfind(", "))
            tail = trimmed[:trimmed.rfind(":")]
            trimmed = tail[:tail.rfind(" ", 0, len(tail))] if cut < 0 else trimmed[:cut + 1]
            text = trimmed.rstrip(" ,:") + ("" if trimmed.rstrip().endswith((".", "।")) else ".")
    return text


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return phone[:3] + "x" * max(0, len(phone) - 6) + phone[-3:]


# ── Providers ────────────────────────────────────────────────────────────────

@dataclass
class ProviderResult:
    ok: bool
    ref: str | None = None
    error: str | None = None


def _msisdn(phone: str) -> str:
    return phone.lstrip("+")   # MSG91 wants 91XXXXXXXXXX


def _msg91_template_id(template: str) -> str:
    return getattr(settings, f"msg91_tpl_{template}", "") or ""


def _msg91_result(resp: httpx.Response) -> ProviderResult:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200 and str(data.get("type", "")).lower() == "success":
        return ProviderResult(True, ref=str(data.get("message") or data.get("request_id") or ""))
    # Never echo the request (it carries the auth key) - only what MSG91 said.
    msg = str(data.get("message") or data.get("errors") or resp.text)[:200]
    return ProviderResult(False, error=f"msg91_http_{resp.status_code}:{msg}")


def msg91_send_flow(phone: str, template: str, language: str, variables: dict,
                    client: httpx.Client | None = None) -> ProviderResult:
    tpl_id = _msg91_template_id(template)
    if not settings.msg91_auth_key:
        return ProviderResult(False, error="msg91_auth_key_not_set")
    if not tpl_id:
        return ProviderResult(False, error=f"msg91_template_not_configured:{template}")
    recipient = {"mobiles": _msisdn(phone)}
    vals = {"merchant": MERCHANT_NAME, **variables}
    for i, key in enumerate(TEMPLATE_VARS[template], start=1):
        v = vals.get(key, "")
        recipient[f"var{i}"] = _inr(float(v)) if key == "amount" and isinstance(v, (int, float)) else str(v)
    body = {"template_id": tpl_id, "short_url": "0", "recipients": [recipient]}
    if settings.msg91_sender_id:
        body["sender"] = settings.msg91_sender_id
    try:
        c = client or httpx.Client(timeout=8.0)
        resp = c.post("https://control.msg91.com/api/v5/flow/", json=body,
                      headers={"authkey": settings.msg91_auth_key, "accept": "application/json",
                               "content-type": "application/json"})
        return _msg91_result(resp)
    except httpx.HTTPError as e:
        return ProviderResult(False, error=f"msg91_unreachable:{type(e).__name__}")


def msg91_send_otp(phone: str, code: str, expiry_minutes: int,
                   client: httpx.Client | None = None) -> ProviderResult:
    """Our own code is passed to MSG91 (`otp` param), so verification stays in
    app/otp.py (hashed, attempt-capped) and MSG91 is only the courier."""
    if not settings.msg91_auth_key:
        return ProviderResult(False, error="msg91_auth_key_not_set")
    if not settings.msg91_otp_template_id:
        return ProviderResult(False, error="msg91_otp_template_not_configured")
    # authkey goes in a header, not the query string: httpx logs request URLs,
    # and a key in the URL would land in the application log. (MSG91's docs
    # show the query form too; the header form is the one used by its other
    # v5 endpoints. Unverified against the live OTP endpoint - scripts/live_check.py
    # is how to confirm it with your own account.)
    params = {"template_id": settings.msg91_otp_template_id, "mobile": _msisdn(phone),
              "otp": code, "otp_expiry": str(expiry_minutes)}
    try:
        c = client or httpx.Client(timeout=8.0)
        resp = c.post("https://control.msg91.com/api/v5/otp", params=params, json={},
                      headers={"authkey": settings.msg91_auth_key, "accept": "application/json",
                               "content-type": "application/json"})
        return _msg91_result(resp)
    except httpx.HTTPError as e:
        return ProviderResult(False, error=f"msg91_unreachable:{type(e).__name__}")


# ── Public API ───────────────────────────────────────────────────────────────

def provider_name() -> str:
    return "msg91" if settings.messaging_provider == "msg91" else "simulated"


def provider_ready() -> tuple[bool, str | None]:
    if settings.messaging_provider not in ("simulated", "msg91"):
        return False, f"messaging_provider '{settings.messaging_provider}' is not available"
    if settings.environment == "production" and settings.messaging_provider == "simulated":
        return False, "simulated messaging is not allowed in production"
    return True, None


def record_otp_message(db: Session, phone: str, code: str | None, provider: str, status: str,
                       language: str = "hinglish", ref: str | None = None,
                       error: str | None = None) -> OutboundMessage:
    """OTP rows. The code is stored ONLY for the simulated provider (it is the
    demo's phone); a real provider's row never holds the code."""
    mins = max(1, settings.otp_ttl_seconds // 60)
    body = (_OTP_BODY.get(language) or _OTP_BODY["hinglish"]).format(
        code=code if (provider == "simulated" and code) else "••••••", mins=mins)
    row = OutboundMessage(channel="sms", template="otp", subject_type="otp", to_phone=phone,
                          language=language, body=body, provider=provider, status=status,
                          provider_ref=ref, error=error, created_at=clock.now())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def send_message(db: Session, template: str, *, subject_type: str, subject_id: str,
                 phone: str | None, language: str = "hinglish", variables: dict | None = None,
                 link: str | None = None, link_kind: str | None = None,
                 client: httpx.Client | None = None) -> OutboundMessage:
    """Render, store and (if a real provider is configured) deliver one
    message. Never raises: a provider failure becomes status=failed."""
    if template not in TEMPLATE_VARS:
        raise ValueError(f"unknown message template {template}")
    variables = dict(variables or {})
    link = absolute_link(link)
    variables["link"] = link or ""
    body = render(template, language, variables)
    provider = provider_name()

    if not phone:
        status, ref, err = "not_sent_no_contact", None, "no_phone_number_on_file"
    else:
        ok, why = provider_ready()
        if not ok:
            status, ref, err = "failed", None, why
        elif provider == "simulated":
            status, ref, err = "recorded", None, None
        else:
            res = msg91_send_flow(phone, template, language, variables, client)
            status, ref, err = ("sent", res.ref, None) if res.ok else ("failed", None, res.error)

    row = OutboundMessage(
        channel="sms", template=template, subject_type=subject_type, subject_id=str(subject_id),
        to_phone=phone, language=language, body=body, link=link, link_kind=link_kind,
        provider=provider, status=status, provider_ref=ref, error=err, created_at=clock.now())
    db.add(row)
    db.commit()
    db.refresh(row)
    write_audit_entry(db, "outbound_message", {
        "message_id": row.id, "template": template, "subject_type": subject_type,
        "subject_id": str(subject_id), "to": mask_phone(phone), "provider": provider,
        "status": status, "error": err})
    return row


def delivered(msg: OutboundMessage) -> bool:
    """True when the message was handed to the (simulated or real) channel."""
    return msg.status in ("recorded", "sent")

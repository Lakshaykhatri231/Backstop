"""
The one place this app calls api.razorpay.com for Payment Links and card
recurring (mandate) payments.

Why one module: before it, each feature called httpx inline with its own
auth/timeout/error handling, and "no keys" quietly meant "stub" in one place
and "crash" in another. Here:

- Amounts cross the boundary in paise, converted with round_to_paise_safe
  (Razorpay speaks paise, the app stores INR floats - CLAUDE.md conventions).
- Every failure is a RazorpayError with Razorpay's own code/description, so
  callers can audit WHY (a quiet None used to hide the 30-link Test Mode cap).
- Key safety: outside production a live key (rzp_live_) is refused - a demo
  must never move real money by accident. Production refuses a test key in
  config.production_problems instead (startup refusal, not per call).

Storefront checkout (storefront.py) and the drop-off poller (dropoff.py) still
call /v1/orders inline; they predate this and work - moving them is not part of
the Test Mode integration.
"""
import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API = "https://api.razorpay.com/v1"
TIMEOUT = 10.0

# Tests and evals swap this for an httpx.MockTransport (a fake Razorpay) - the
# only seam, so nothing else in the app needs to know it is being faked.
_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


class RazorpayError(Exception):
    def __init__(self, status: int, code: str, description: str):
        super().__init__(f"{status} {code}: {description}")
        self.status, self.code, self.description = status, code, description


class LinkLimitReached(RazorpayError):
    """Test Mode allows a fixed number of Payment Links per business."""


def keys_present() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def is_test_key() -> bool:
    return settings.razorpay_key_id.startswith("rzp_test_")


def to_paise(amount_inr: float) -> int:
    from app.revenue import round_to_paise_safe
    return int(round(round_to_paise_safe(amount_inr) * 100))


def unix(dt: datetime) -> int:
    """Naive datetimes in this app are UTC (datetime.utcnow / clock.now)."""
    return int((dt - datetime(1970, 1, 1)).total_seconds())


def _client() -> httpx.Client:
    if not keys_present():
        raise RazorpayError(0, "no_keys", "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set")
    if settings.razorpay_key_id.startswith("rzp_live_") and settings.environment != "production":
        raise RazorpayError(0, "live_key_outside_production",
                            "a live Razorpay key is refused outside ENVIRONMENT=production")
    return httpx.Client(base_url=API, auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                        timeout=TIMEOUT, transport=_transport)


def _looks_like_link_cap(description: str) -> bool:
    d = description.lower()
    return "limit" in d or "maximum" in d or "exceed" in d


def _call(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    try:
        with _client() as c:
            resp = c.request(method, path, json=body, params=params)
    except httpx.HTTPError as e:
        raise RazorpayError(0, "network_error", str(e)) from e
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {}) or {}
        except ValueError:
            err = {}
        code = err.get("code") or "http_error"
        desc = err.get("description") or resp.text[:300]
        if method == "POST" and path == "/payment_links" and _looks_like_link_cap(desc):
            raise LinkLimitReached(resp.status_code, code, desc)
        raise RazorpayError(resp.status_code, code, desc)
    try:
        return resp.json()
    except ValueError as e:
        raise RazorpayError(resp.status_code, "bad_json", resp.text[:300]) from e


# ── Payment Links ────────────────────────────────────────────────────────────

def create_payment_link(*, amount_inr: float, reference_id: str, description: str,
                        expire_at: datetime, notes: dict, customer: dict | None = None,
                        accept_partial: bool = False, first_min_partial_inr: float | None = None) -> dict:
    """notify/reminders are always OFF: messaging.py is the only place a
    customer text leaves the system, so Razorpay must not send its own."""
    amount = to_paise(amount_inr)
    body = {
        "amount": amount, "currency": "INR", "reference_id": reference_id[:40],
        "description": description[:2048], "expire_by": unix(expire_at),
        "notes": {k: str(v)[:256] for k, v in list(notes.items())[:15]},
        "notify": {"sms": False, "email": False}, "reminder_enable": False,
    }
    if customer:
        body["customer"] = {k: v for k, v in customer.items() if v}
    if accept_partial:
        body["accept_partial"] = True
        body["first_min_partial_amount"] = min(amount, to_paise(first_min_partial_inr or 100.0))
    return _call("POST", "/payment_links", body)


def cancel_payment_link(link_id: str) -> dict:
    return _call("POST", f"/payment_links/{link_id}/cancel")


def update_payment_link_expiry(link_id: str, expire_at: datetime) -> dict:
    """Move an OPEN link's expiry (PATCH, only valid while it is `created` or
    `partially_paid`). Same link id, nothing new is created."""
    return _call("PATCH", f"/payment_links/{link_id}", {"expire_by": unix(expire_at)})


def fetch_payment_link(link_id: str) -> dict:
    return _call("GET", f"/payment_links/{link_id}")


def list_payment_links(count: int = 100, skip: int = 0) -> list[dict]:
    data = _call("GET", "/payment_links", params={"count": count, "skip": skip})
    return data.get("payment_links") or data.get("items") or []


def fetch_payment(payment_id: str) -> dict:
    return _call("GET", f"/payments/{payment_id}")


def refund_payment(payment_id: str, amount_inr: float | None = None, notes: dict | None = None) -> dict:
    body: dict = {"notes": notes or {}}
    if amount_inr is not None:
        body["amount"] = to_paise(amount_inr)
    return _call("POST", f"/payments/{payment_id}/refund", body)


# ── Card recurring (mandates) ────────────────────────────────────────────────

def create_customer(*, name: str, contact: str | None, email: str | None, notes: dict) -> dict:
    """fail_existing=0: an existing customer with the same contact/email is
    returned instead of an error, so a re-run never duplicates customers."""
    body = {"name": name[:50], "fail_existing": "0", "notes": notes}
    if contact:
        body["contact"] = contact
    if email:
        body["email"] = email
    return _call("POST", "/customers", body)


def create_registration_link(*, customer: dict, description: str, max_amount_inr: float,
                             frequency: str, token_expire_at: datetime, link_expire_at: datetime,
                             receipt: str, notes: dict) -> dict:
    """Razorpay-hosted page where the customer authorises a card mandate
    (₹1 authorisation payment). Returns an invoice object (inv_...) with
    short_url; it is not a Payment Link, so it does not use the 30-link cap."""
    body = {
        "customer": {k: v for k, v in customer.items() if v},
        "type": "link", "amount": 100, "currency": "INR", "description": description[:2048],
        "subscription_registration": {
            "method": "card", "max_amount": to_paise(max_amount_inr),
            "expire_at": unix(token_expire_at), "frequency": frequency,
        },
        "receipt": receipt[:40], "email_notify": False, "sms_notify": False,
        "expire_by": unix(link_expire_at), "notes": notes,
    }
    return _call("POST", "/subscription_registration/auth_links", body)


def fetch_invoice(invoice_id: str) -> dict:
    return _call("GET", f"/invoices/{invoice_id}")


def list_tokens(customer_id: str) -> list[dict]:
    data = _call("GET", f"/customers/{customer_id}/tokens")
    return data.get("items") or []


def fetch_token(customer_id: str, token_id: str) -> dict:
    return _call("GET", f"/customers/{customer_id}/tokens/{token_id}")


def cancel_token(customer_id: str, token_id: str) -> dict:
    return _call("DELETE", f"/customers/{customer_id}/tokens/{token_id}")


def create_debit_order(*, amount_inr: float, token_id: str, debit_at: datetime,
                       receipt: str, notes: dict) -> dict:
    """The order for one recurring debit. Its `notification` object makes
    Razorpay send the bank pre-debit notice now and refuse the debit before
    payment_after. With a notification object Razorpay does NOT auto-retry a
    failed debit - every retry is ours (that is the point of this integration)."""
    body = {
        "amount": to_paise(amount_inr), "currency": "INR", "payment_capture": True,
        "receipt": receipt[:40], "notes": notes,
        "notification": {"token_id": token_id, "payment_after": unix(debit_at)},
    }
    return _call("POST", "/orders", body)


def create_recurring_payment(*, amount_inr: float, order_id: str, customer_id: str, token_id: str,
                             email: str, contact: str, description: str, notes: dict) -> dict:
    body = {
        "email": email, "contact": contact, "amount": to_paise(amount_inr), "currency": "INR",
        "order_id": order_id, "customer_id": customer_id, "token": token_id,
        "recurring": True, "description": description[:255], "notes": notes,
    }
    return _call("POST", "/payments/create/recurring", body)

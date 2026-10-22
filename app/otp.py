"""
Phone verification by one-time code.

Why it exists: a voice call is only allowed to a number the person has proved
is theirs, and consent can only be recorded against such a number - so typing
someone else's number can never get them called.

- Codes are 6 digits, expire (default 5 min), and are stored as an HMAC, never
  in clear. A challenge dies on success, expiry, or too many wrong guesses.
- Abuse limits: a resend cooldown and a per-phone hourly send cap, so the
  endpoint cannot be used to spam a number with SMS.
- A successful check returns a short-lived signed PROOF bound to
  (phone, purpose). Registration / profile / self-serve submit then present
  that proof; nothing is trusted from the client otherwise.

Provider: `simulated` sends nothing - the code is returned in the API response
(flagged `simulated`) AND stored in the Demo Console inbox, so the demo is
usable and the "SMS" is visible. `msg91` sends the code through MSG91's OTP
API (our own code is passed in, so hashing / attempt limits stay here); in
India that needs DLT template registration, a deployment task. The code is
NEVER returned or stored in clear when a real provider is configured.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app import clock, messaging
from app.audit import write_audit_entry
from app.config import settings
from app.models import OtpChallenge

logger = logging.getLogger(__name__)

PROOF_TTL_SECONDS = 15 * 60
_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")


class OtpError(Exception):
    """Carries a stable machine code plus a human message."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ── Phone ────────────────────────────────────────────────────────────────────

def normalize_phone(raw: str | None) -> str:
    """Indian mobile -> E.164 ("+91XXXXXXXXXX"). Accepts 10 digits, 0/91/+91
    prefixes and common separators; anything else is rejected, not guessed."""
    digits = re.sub(r"[\s\-().]", "", raw or "")
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if not _INDIAN_MOBILE.match(digits):
        raise OtpError("invalid_phone", "Enter a valid 10-digit Indian mobile number.")
    return "+91" + digits


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return phone[:3] + "x" * max(0, len(phone) - 6) + phone[-3:]


# ── Provider ─────────────────────────────────────────────────────────────────

class OtpProvider(Protocol):
    simulated: bool

    def send(self, phone: str, code: str) -> None: ...


class SimulatedOtpProvider:
    simulated = True

    def send(self, phone: str, code: str) -> None:
        # Deliberately no SMS and no code in the log - the API response
        # carries it in simulated mode only.
        logger.info("simulated OTP issued for %s", mask_phone(phone))


class Msg91OtpProvider:
    simulated = False

    def __init__(self) -> None:
        self.ref: str | None = None

    def send(self, phone: str, code: str) -> None:
        res = messaging.msg91_send_otp(phone, code, max(1, settings.otp_ttl_seconds // 60))
        if not res.ok:
            logger.warning("msg91 OTP send failed for %s: %s", mask_phone(phone), res.error)
            raise OtpError("provider_error",
                           "We could not send the code right now. Please try again.", 502)
        self.ref = res.ref


def _provider() -> OtpProvider:
    """The configured provider. Anything unrecognised is refused outright -
    silently falling back to the simulated provider would hand the one-time
    code back in the API response, i.e. turn the verification into theatre."""
    if settings.otp_provider == "msg91":
        if not (settings.msg91_auth_key and settings.msg91_otp_template_id):
            raise OtpError("provider_not_configured",
                           "MSG91 needs MSG91_AUTH_KEY and MSG91_OTP_TEMPLATE_ID.", 503)
        return Msg91OtpProvider()
    if settings.otp_provider == "simulated":
        if settings.environment == "production":
            raise OtpError("provider_not_configured",
                           "The simulated OTP provider is not allowed in production; "
                           "configure a real SMS provider.", 503)
        return SimulatedOtpProvider()
    raise OtpError("provider_not_configured",
                   f"OTP provider '{settings.otp_provider}' is not available.", 503)


# ── Hashing / proof ──────────────────────────────────────────────────────────

def _hash_code(phone: str, purpose: str, code: str) -> str:
    msg = f"otp:{phone}:{purpose}:{code}".encode()
    return hmac.new(settings.storefront_secret_key.encode(), msg, hashlib.sha256).hexdigest()


def _sign(body_b64: str) -> str:
    return hmac.new(settings.storefront_secret_key.encode(), f"proof:{body_b64}".encode(),
                    hashlib.sha256).hexdigest()


def make_proof(phone: str, purpose: str) -> str:
    body = json.dumps({"p": phone, "u": purpose, "exp": int(time.time()) + PROOF_TTL_SECONDS}).encode()
    body_b64 = base64.urlsafe_b64encode(body).decode()
    return f"{body_b64}.{_sign(body_b64)}"


def check_proof(proof: str | None, phone: str, purpose: str) -> bool:
    """True only for an unexpired proof issued for exactly this phone+purpose."""
    try:
        body_b64, sig = (proof or "").split(".")
        if not hmac.compare_digest(sig, _sign(body_b64)):
            return False
        data = json.loads(base64.urlsafe_b64decode(body_b64.encode()))
        return data["p"] == phone and data["u"] == purpose and data["exp"] >= time.time()
    except Exception:
        return False


# ── Send / verify ────────────────────────────────────────────────────────────

def send_otp(db: Session, phone: str, purpose: str, now: datetime | None = None) -> dict:
    """Issue a code. Returns {"sent": True, "expires_in", "simulated", "dev_code"?}."""
    now = now or clock.now()
    # Resolve the provider FIRST: a refused provider (e.g. the simulated one
    # in production) must fail before anything is written or rate-limited.
    provider = _provider()
    last = (
        db.query(OtpChallenge)
        .filter(OtpChallenge.phone == phone).order_by(OtpChallenge.created_at.desc()).first()
    )
    if last and (now - last.created_at).total_seconds() < settings.otp_resend_cooldown_seconds:
        wait = int(settings.otp_resend_cooldown_seconds - (now - last.created_at).total_seconds()) + 1
        raise OtpError("cooldown", f"Please wait {wait}s before requesting another code.", 429)
    sent_last_hour = db.query(OtpChallenge).filter(
        OtpChallenge.phone == phone, OtpChallenge.created_at > now - timedelta(hours=1)).count()
    if sent_last_hour >= settings.otp_max_sends_per_phone_per_hour:
        raise OtpError("rate_limited", "Too many codes requested for this number. Try again later.", 429)

    code = f"{secrets.randbelow(10**6):06d}"
    challenge = OtpChallenge(
        phone=phone, purpose=purpose, code_hash=_hash_code(phone, purpose, code),
        expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
    )
    db.add(challenge)
    db.commit()
    name = "simulated" if provider.simulated else "msg91"
    try:
        provider.send(phone, code)
    except OtpError as e:
        # Nothing reached the customer, so the challenge (and its place in the
        # hourly send cap) must not count.
        db.delete(challenge)
        db.commit()
        messaging.record_otp_message(db, phone, None, name, "failed", error=e.code)
        raise
    messaging.record_otp_message(db, phone, code, name, "recorded" if provider.simulated else "sent",
                                 ref=getattr(provider, "ref", None))
    write_audit_entry(db, "otp_sent", {"phone": mask_phone(phone), "purpose": purpose,
                                       "simulated": provider.simulated})
    out = {"sent": True, "expires_in": settings.otp_ttl_seconds, "simulated": provider.simulated}
    if provider.simulated:
        out["dev_code"] = code   # demo only; never present with a real provider
    return out


def verify_otp(db: Session, phone: str, purpose: str, code: str,
               now: datetime | None = None) -> str:
    """Check a code; returns a signed proof on success."""
    now = now or clock.now()
    ch = (
        db.query(OtpChallenge)
        .filter(OtpChallenge.phone == phone, OtpChallenge.purpose == purpose,
                OtpChallenge.consumed_at.is_(None))
        .order_by(OtpChallenge.created_at.desc()).first()
    )
    if ch is None or ch.expires_at < now:
        raise OtpError("expired", "That code has expired. Request a new one.")
    if ch.attempts >= settings.otp_max_attempts:
        raise OtpError("too_many_attempts", "Too many wrong attempts. Request a new code.", 429)
    ch.attempts += 1
    ok = hmac.compare_digest(ch.code_hash, _hash_code(phone, purpose, (code or "").strip()))
    if not ok:
        db.commit()
        raise OtpError("wrong_code", "That code is not correct.")
    ch.consumed_at = now
    db.commit()
    write_audit_entry(db, "otp_verified", {"phone": mask_phone(phone), "purpose": purpose})
    return make_proof(phone, purpose)

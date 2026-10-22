"""
Deliberately thin auth. This project isn't about auth security - it exists
only so that cart/checkout/cart-abandonment actions can be tied to a real
Customer row, which is what makes tiering and order history possible.

- Passwords: salted PBKDF2 hash (stdlib hashlib, no extra dependency).
- Sessions: a signed token (customer_id + expiry, HMAC-signed) carried as a
  Bearer token. Not a full JWT implementation - just enough to be tamper-evident.
"""
import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Customer

TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 days - long enough that a demo session never expires mid-pitch


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return hmac.compare_digest(actual, expected)


def _sign(payload: bytes) -> str:
    return hmac.new(settings.storefront_secret_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def create_token(customer_id: str, role: str = "customer") -> str:
    """`role` is customer (a storefront shopper, `customers` row), merchant or
    business (`app_users` rows). It is signed into the token, never taken from
    the request, and a token without one is a customer's (older tokens)."""
    body = json.dumps({"cid": customer_id, "role": role,
                       "exp": int(time.time()) + TOKEN_TTL_SECONDS}).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(body).decode()
    sig = _sign(body_b64.encode("utf-8"))
    return f"{body_b64}.{sig}"


def decode_claims(token: str) -> tuple[str, str] | None:
    """(subject id, role) for a valid, unexpired token, else None."""
    try:
        body_b64, sig = token.split(".")
        expected_sig = _sign(body_b64.encode("utf-8"))
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body_b64.encode("utf-8")))
        if payload["exp"] < time.time():
            return None
        return payload["cid"], payload.get("role", "customer")
    except Exception:
        return None


def decode_token(token: str) -> str | None:
    """Customer id for a SHOPPER token. A merchant / business token is not a
    customer's, so the storefront routes that depend on this reject it."""
    claims = decode_claims(token)
    return claims[0] if claims and claims[1] == "customer" else None


def get_current_customer(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Customer:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    customer_id = decode_token(token)
    if not customer_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=401, detail="Customer no longer exists")
    return customer


# ── Merchant / business logins (app_users) ──────────────────────────────────

def bearer_claims(authorization: str) -> tuple[str, str] | None:
    if not authorization.startswith("Bearer "):
        return None
    return decode_claims(authorization.removeprefix("Bearer ").strip())


def require_role(*roles: str):
    """Dependency: a valid token whose role is one of `roles`, resolved to its
    `app_users` row (merchant / business). Shoppers use get_current_customer."""
    def dep(authorization: str = Header(default=""), db: Session = Depends(get_db)):
        from app.models import AppUser
        claims = bearer_claims(authorization)
        if claims is None:
            raise HTTPException(status_code=401, detail="Sign in first")
        sid, role = claims
        if role not in roles:
            raise HTTPException(status_code=403, detail="This page is not for your account type")
        user = db.query(AppUser).filter(AppUser.id == sid, AppUser.role == role).first()
        if user is None:
            raise HTTPException(status_code=401, detail="Account no longer exists")
        return user
    return dep


def is_merchant_bearer(authorization: str) -> bool:
    """True for a token the merchant signed in with (checked against the DB by
    the caller where it matters; this only reads the signed role)."""
    claims = bearer_claims(authorization)
    return bool(claims and claims[1] == "merchant")

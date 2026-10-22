"""
Opt-in access key for the merchant side.

The project's merchant dashboard has always been open (CLAUDE.md: "no auth").
That is fine for a local demo and not fine for anything reachable. Rather than
bolt a full login system onto a demo, this adds one shared secret that is
OFF by default: leave MERCHANT_API_KEY empty and nothing changes; set it and
every merchant-side endpoint requires an `X-Merchant-Key` header (the
dashboard asks for the key once and remembers it). A merchant LOGIN (app_users,
role merchant, see app/accounts.py) is accepted in its place, and once one exists
the endpoints require it (or the key) even with no key configured.

Deliberately NOT protected, because someone other than the merchant must reach
them: Razorpay webhooks (own HMAC signature), customer routes (own Bearer
token), the OTP and self-serve endpoints (signed link / phone proof), the
storefront's demo levers, and /merchant/auth-status (which only reports
whether a key is needed).

This is a shared secret, not per-user auth: no roles, no audit of who did
what, and no rate limit on guesses - use a long random value, and put real
authentication in front of this before exposing it to the internet.
"""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

router = APIRouter()


def _matches(supplied: str) -> bool:
    return hmac.compare_digest((supplied or "").encode(), settings.merchant_api_key.encode())


def _merchant_login_ok(db: Session, authorization: str) -> bool:
    """A Bearer token the merchant signed in with (app_users, role merchant)."""
    from app import auth
    from app.models import AppUser
    claims = auth.bearer_claims(authorization)
    if claims is None or claims[1] != "merchant":
        return False
    return db.query(AppUser.id).filter(AppUser.id == claims[0], AppUser.role == "merchant").first() is not None


def _merchant_exists(db: Session) -> bool:
    from app.models import AppUser
    return db.query(AppUser.id).filter(AppUser.role == "merchant").first() is not None


def access_state(db: Session, x_merchant_key: str, authorization: str) -> tuple[bool, bool]:
    """(required, ok). Required when a key is configured OR a merchant account
    exists; OK with the key or the merchant's own sign-in. With neither set up
    the dashboard stays open, so a fresh clone still demos."""
    required = bool(settings.merchant_api_key) or _merchant_exists(db)
    if not required:
        return False, True
    if settings.merchant_api_key and _matches(x_merchant_key):
        return True, True
    return True, _merchant_login_ok(db, authorization)


def require_merchant(x_merchant_key: str = Header(default=""), authorization: str = Header(default=""),
                     db: Session = Depends(get_db)) -> None:
    """FastAPI dependency for merchant-side routes."""
    required, ok = access_state(db, x_merchant_key, authorization)
    if required and not ok:
        raise HTTPException(
            status_code=401,
            detail={"code": "merchant_key_required",
                    "message": "Sign in as the merchant (or send a valid X-Merchant-Key) to use this endpoint."},
        )


@router.get("/merchant/auth-status")
def auth_status(x_merchant_key: str = Header(default=""), authorization: str = Header(default=""),
                db: Session = Depends(get_db)):
    required, ok = access_state(db, x_merchant_key, authorization)
    return {"required": required, "ok": ok, "key_configured": bool(settings.merchant_api_key),
            "merchant_login": _merchant_exists(db)}

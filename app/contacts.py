"""
Contact points: who we may call, on which number, in which language.

One table (models.ContactPoint) answers "can we call this person?" for every
kind of debt - a failed storefront order, a failed mandate debit, an overdue
invoice - so voice never has per-subject contact logic.

Rules (hardcoded, not tunable - a policy knob here is a compliance bug):
- A call needs a phone VERIFIED by OTP and consent the CUSTOMER ticked
  themselves. There is no merchant-attested path.
- Consent can only be recorded against a verified phone, and is wiped if the
  number changes (consent was given for that number, not the next one).
- Withdrawing consent is always allowed, no OTP needed.
- A merchant supplies number + language only; the merchant endpoints cannot
  set consent.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import otp as otp_service
from app.audit import write_audit_entry
from app.auth import get_current_customer
from app.database import get_db
from app.merchant_auth import require_merchant
from app.models import BusinessAccount, ContactPoint, Customer
from app.otp import OtpError, mask_phone, normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_LANGUAGES = ("en", "hi", "hinglish")
# Hinglish is the default for everyone; the customer can switch to Hindi or
# English at any time (registration, profile, or the self-serve page).
DEFAULT_LANGUAGE = "hinglish"
CONSENT_TEXT_VERSION = "v1"
CONSENT_TEXT = (
    "I agree that this store may call me on this number about a payment of mine that failed "
    "or is pending. I can withdraw this at any time."
)
PHONE_PURPOSE_LINE = "Used only to contact you about a failed payment. Never for marketing."


# ── Resolution ───────────────────────────────────────────────────────────────

def for_customer(db: Session, customer_id: str) -> ContactPoint | None:
    return db.query(ContactPoint).filter(ContactPoint.customer_id == customer_id).first()


def for_razorpay_customer(db: Session, razorpay_customer_id: str | None) -> ContactPoint | None:
    if not razorpay_customer_id:
        return None
    return db.query(ContactPoint).filter(ContactPoint.razorpay_customer_id == razorpay_customer_id).first()


def for_business_account(db: Session, account_id: str) -> ContactPoint | None:
    return db.query(ContactPoint).filter(ContactPoint.business_account_id == account_id).first()


def can_call(cp: ContactPoint | None) -> tuple[bool, str | None]:
    """(allowed, block_reason). Window / daily / attempt guards live in voice.py."""
    if cp is None or not cp.phone:
        return False, "no_phone_number"
    if cp.wrong_number_at is not None:
        return False, "wrong_number_flagged"
    if cp.phone_verified_at is None:
        return False, "phone_not_verified"
    if not cp.voice_consent or cp.consent_source != "customer_ticked":
        return False, "no_voice_consent"
    return True, None


# ── Writes ───────────────────────────────────────────────────────────────────

def _mirror_to_customer(db: Session, cp: ContactPoint) -> None:
    """Keep the Phase-0 convenience columns on Customer in step."""
    if not cp.customer_id:
        return
    c = db.query(Customer).filter(Customer.id == cp.customer_id).first()
    if c is None:
        return
    c.phone = cp.phone
    c.preferred_language = cp.preferred_language
    c.voice_consent = bool(cp.voice_consent)
    c.consent_at = cp.consent_at
    db.commit()


def upsert_contact(
    db: Session,
    *,
    customer_id: str | None = None,
    razorpay_customer_id: str | None = None,
    business_account_id: str | None = None,
    phone: str | None = None,
    language: str | None = None,
    verified_now: bool = False,
) -> ContactPoint:
    """Create/update the contact for an identity. `phone` must already be
    normalised. A changed number clears verification AND consent."""
    cp = None
    if customer_id:
        cp = for_customer(db, customer_id)
    if cp is None and razorpay_customer_id:
        cp = for_razorpay_customer(db, razorpay_customer_id)
    if cp is None and business_account_id:
        cp = for_business_account(db, business_account_id)
    if cp is None:
        cp = ContactPoint(customer_id=customer_id, razorpay_customer_id=razorpay_customer_id,
                          business_account_id=business_account_id)
        db.add(cp)
    else:
        # link any identity we newly learned about
        cp.customer_id = cp.customer_id or customer_id
        cp.razorpay_customer_id = cp.razorpay_customer_id or razorpay_customer_id
        cp.business_account_id = cp.business_account_id or business_account_id

    if phone is not None and phone != cp.phone:
        if cp.phone:   # an actual change, not a first set
            write_audit_entry(db, "contact_phone_changed", {
                "contact_id": cp.id, "old": mask_phone(cp.phone), "new": mask_phone(phone),
                "consent_cleared": bool(cp.voice_consent)})
        cp.phone = phone
        cp.phone_verified_at = None
        cp.voice_consent = False
        cp.consent_at = None
        cp.consent_source = None
        cp.consent_text_version = None
        cp.wrong_number_at = None
        if cp.memory_consent:          # a different number may be a different person: forget what we noted
            cp.memory_consent = False
            cp.memory_consent_at = None
            cp.memory_consent_version = None
            from app import memory
            memory.forget(db, cp.id, reason="phone_changed")
    if verified_now and phone is not None:
        cp.phone_verified_at = datetime.utcnow()
    if language is not None:
        if language not in VALID_LANGUAGES:
            raise HTTPException(422, "preferred_language must be one of en, hi, hinglish")
        cp.preferred_language = language
    db.commit()
    db.refresh(cp)
    _mirror_to_customer(db, cp)
    return cp


def record_consent(db: Session, cp: ContactPoint, consent: bool, via: str) -> ContactPoint:
    """Customer ticked (or un-ticked) the box. Granting needs a verified phone."""
    if consent:
        if cp.phone_verified_at is None:
            raise OtpError("phone_not_verified",
                           "Verify your phone number before agreeing to calls.", 409)
        cp.voice_consent = True
        cp.consent_at = datetime.utcnow()
        cp.consent_source = "customer_ticked"
        cp.consent_text_version = CONSENT_TEXT_VERSION
        write_audit_entry(db, "voice_consent_given", {
            "contact_id": cp.id, "phone": mask_phone(cp.phone), "via": via,
            "text_version": CONSENT_TEXT_VERSION})
    else:
        if cp.voice_consent:
            write_audit_entry(db, "voice_consent_withdrawn", {"contact_id": cp.id, "via": via})
        cp.voice_consent = False
        cp.consent_at = None
        cp.consent_source = None
        cp.consent_text_version = None
    db.commit()
    db.refresh(cp)
    _mirror_to_customer(db, cp)
    return cp


# v1 (notes only) -> v2 (also keeps a record of each conversation). Tiered memory writes - episodes, the
# word-for-word archive, facts - need v2: someone who agreed to "short notes" has NOT agreed to a transcript,
# so they must tick the new wording before anything beyond the v1 notes is kept about them.
MEMORY_CONSENT_TEXT_VERSION = "v2"
MEMORY_CONSENT_TEXT = (
    "Remember our calls so future calls are easier: keep short notes (for example a convenient time to call, "
    "or when your salary arrives) and a record of what was said on each call. It is used only to help our "
    "future calls with you, and you can ask to have all of it deleted at any time."
)


def has_full_memory_consent(cp: ContactPoint | None) -> bool:
    """Consent to the CURRENT memory wording (v2). v1 contacts keep the old notes behaviour only."""
    return bool(cp is not None and cp.memory_consent and cp.memory_consent_version == MEMORY_CONSENT_TEXT_VERSION)


def record_memory_consent(db: Session, cp: ContactPoint, consent: bool, via: str) -> ContactPoint:
    """Customer ticked / un-ticked "remember our calls". Granting needs a verified phone.
    Withdrawing deletes everything kept about them, in the same step."""
    if consent:
        if cp.phone_verified_at is None:
            raise OtpError("phone_not_verified", "Verify your phone number first.", 409)
        if not has_full_memory_consent(cp):       # first grant, or a v1 contact agreeing to the new wording
            upgrading = bool(cp.memory_consent)
            cp.memory_consent = True
            cp.memory_consent_at = datetime.utcnow()
            cp.memory_consent_version = MEMORY_CONSENT_TEXT_VERSION
            write_audit_entry(db, "memory_consent_given", {"contact_id": cp.id, "via": via,
                                                           "text_version": MEMORY_CONSENT_TEXT_VERSION})
            if upgrading:                         # carry the old "short notes" over as facts
                from app import memory_write
                db.flush()
                memory_write.migrate_notes(db, cp)
    else:
        if cp.memory_consent:
            write_audit_entry(db, "memory_consent_withdrawn", {"contact_id": cp.id, "via": via})
        cp.memory_consent = False
        cp.memory_consent_at = None
        cp.memory_consent_version = None
        from app import memory          # local: memory imports contacts
        memory.forget(db, cp.id, reason="consent_withdrawn")
    db.commit()
    db.refresh(cp)
    return cp


def contact_out(cp: ContactPoint | None) -> dict:
    if cp is None:
        return {"has_phone": False, "phone_masked": "", "phone_verified": False,
                "preferred_language": DEFAULT_LANGUAGE, "voice_consent": False, "can_call": False,
                "block_reason": "no_phone_number"}
    ok, reason = can_call(cp)
    return {
        "has_phone": bool(cp.phone), "phone_masked": mask_phone(cp.phone),
        "phone_verified": cp.phone_verified_at is not None,
        "preferred_language": cp.preferred_language, "voice_consent": bool(cp.voice_consent),
        "memory_consent": bool(cp.memory_consent),
        "memory_consent_current": has_full_memory_consent(cp),     # False for a v1 contact: ask again
        "consent_at": cp.consent_at, "can_call": ok, "block_reason": reason,
    }


def _otp_http(e: OtpError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


# ── Public: pre-registration phone verification ──────────────────────────────

class PhoneIn(BaseModel):
    phone: str


class PhoneCodeIn(BaseModel):
    phone: str
    code: str


@router.get("/contacts/consent-text")
def consent_text():
    """The exact wording the UI must show - the version recorded with a
    consent is this one, so what was agreed to is always reconstructable."""
    return {"version": CONSENT_TEXT_VERSION, "text": CONSENT_TEXT,
            "phone_purpose": PHONE_PURPOSE_LINE}


@router.post("/auth/phone/otp/send")
def register_otp_send(req: PhoneIn, db: Session = Depends(get_db)):
    try:
        return otp_service.send_otp(db, normalize_phone(req.phone), "register")
    except OtpError as e:
        raise _otp_http(e)


@router.post("/auth/phone/otp/verify")
def register_otp_verify(req: PhoneCodeIn, db: Session = Depends(get_db)):
    try:
        phone = normalize_phone(req.phone)
        return {"phone": phone, "phone_proof": otp_service.verify_otp(db, phone, "register", req.code)}
    except OtpError as e:
        raise _otp_http(e)


# ── Logged-in customer: profile contact ──────────────────────────────────────

class ProfileContactIn(BaseModel):
    preferred_language: str | None = None
    voice_consent: bool | None = None


@router.get("/me/contact")
def my_contact(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    return contact_out(for_customer(db, customer.id))


@router.post("/me/contact/otp/send")
def my_contact_otp_send(req: PhoneIn, customer: Customer = Depends(get_current_customer),
                        db: Session = Depends(get_db)):
    try:
        return otp_service.send_otp(db, normalize_phone(req.phone), "profile")
    except OtpError as e:
        raise _otp_http(e)


@router.post("/me/contact/verify")
def my_contact_verify(req: PhoneCodeIn, customer: Customer = Depends(get_current_customer),
                      db: Session = Depends(get_db)):
    """Verify a (new) phone for this account; changing the number clears consent."""
    try:
        phone = normalize_phone(req.phone)
        otp_service.verify_otp(db, phone, "profile", req.code)
    except OtpError as e:
        raise _otp_http(e)
    cp = upsert_contact(db, customer_id=customer.id, phone=phone, verified_now=True)
    if cp.phone_verified_at is None:   # same number, previously unverified
        cp.phone_verified_at = datetime.utcnow()
        db.commit()
    return contact_out(cp)


@router.put("/me/contact")
def my_contact_update(req: ProfileContactIn, customer: Customer = Depends(get_current_customer),
                      db: Session = Depends(get_db)):
    cp = for_customer(db, customer.id)
    if cp is None:
        raise HTTPException(409, detail={"code": "no_phone_number",
                                         "message": "Add and verify a phone number first."})
    if req.preferred_language is not None:
        upsert_contact(db, customer_id=customer.id, language=req.preferred_language)
    if req.voice_consent is not None:
        try:
            record_consent(db, cp, req.voice_consent, via="storefront_profile")
        except OtpError as e:
            raise _otp_http(e)
    return contact_out(for_customer(db, customer.id))


# ── Merchant dashboard: number + language only ───────────────────────────────

class MerchantContactIn(BaseModel):
    razorpay_customer_id: str | None = None
    business_account_id: str | None = None
    phone: str
    preferred_language: str = DEFAULT_LANGUAGE


def _merchant_upsert(db: Session, row: MerchantContactIn) -> ContactPoint:
    if not (row.razorpay_customer_id or row.business_account_id):
        raise HTTPException(422, "Provide razorpay_customer_id or business_account_id")
    if row.business_account_id and not db.query(BusinessAccount).filter(
            BusinessAccount.id == row.business_account_id).first():
        raise HTTPException(404, "Business account not found")
    try:
        phone = normalize_phone(row.phone)
    except OtpError as e:
        raise _otp_http(e)
    # NB: no consent argument exists on this path, by design.
    return upsert_contact(db, razorpay_customer_id=row.razorpay_customer_id,
                          business_account_id=row.business_account_id,
                          phone=phone, language=row.preferred_language)


@router.post("/contacts", dependencies=[Depends(require_merchant)])
def merchant_set_contact(req: MerchantContactIn, db: Session = Depends(get_db)):
    cp = _merchant_upsert(db, req)
    write_audit_entry(db, "contact_set_by_merchant", {
        "contact_id": cp.id, "phone": mask_phone(cp.phone),
        "razorpay_customer_id": cp.razorpay_customer_id, "business_account_id": cp.business_account_id})
    return {"id": cp.id, **contact_out(cp)}


class BulkContactsIn(BaseModel):
    contacts: list[MerchantContactIn]


@router.post("/contacts/bulk", dependencies=[Depends(require_merchant)])
def merchant_set_contacts_bulk(req: BulkContactsIn, db: Session = Depends(get_db)):
    results = []
    for row in req.contacts[:2000]:
        try:
            cp = _merchant_upsert(db, row)
            results.append({"phone": mask_phone(cp.phone), "status": "saved", "id": cp.id})
        except HTTPException as e:
            results.append({"phone": mask_phone(row.phone), "status": "rejected", "detail": e.detail})
    return {"results": results}


@router.get("/contacts", dependencies=[Depends(require_merchant)])
def list_contacts(db: Session = Depends(get_db)):
    rows = db.query(ContactPoint).order_by(ContactPoint.created_at.desc()).limit(500).all()
    return [{
        "id": c.id, "razorpay_customer_id": c.razorpay_customer_id,
        "customer_id": c.customer_id, "business_account_id": c.business_account_id,
        **contact_out(c),
    } for c in rows]

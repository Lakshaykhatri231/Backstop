"""
Logins and the pages behind them.

Three kinds of person sign in (all through POST /auth/login):

  customer  a storefront shopper (`customers`). Gets /account: start autopay for
            a service, give the permissions we ask for, see the next debit.
  business  an invoice customer the merchant registered (`app_users`). Gets
            /account/invoices: what they owe, a Pay now button, the same
            permissions card.
  merchant  the one merchant (`app_users`). Registers business customers and
            invoices; everything merchant-side accepts this sign-in.

Permissions are the same ones the rest of the app already enforces
(`contacts.can_call`, memory consent): nothing here can grant them on a
person's behalf - the person's own request does, after the number is verified.
"""
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import autopay, autopay_services, contacts, otp as otp_service, razorpay_client as rz
from app.audit import write_audit_entry
from app.auth import get_current_customer, hash_password, require_role, verify_password
from app.database import get_db
from app.merchant_auth import require_merchant
from app.models import (
    AppUser, AuditLog, BusinessAccount, Customer, Invoice, Mandate, MandateState, ScheduledAction,
    ScheduledKind, ScheduledStatus,
)
from app.otp import OtpError

logger = logging.getLogger(__name__)
router = APIRouter()

LANGUAGE_LABEL = {"en": "English", "hi": "Hindi", "hinglish": "Hinglish"}


# ── Merchant account ─────────────────────────────────────────────────────────

def bootstrap_merchant(db: Session) -> str | None:
    """Create the merchant login from MERCHANT_EMAIL / MERCHANT_PASSWORD on
    first start (nothing is created when they are unset, so a fresh clone keeps
    its open dashboard). Never changes an existing password."""
    from app.config import settings
    email = (settings.merchant_email or "").strip().lower()
    password = settings.merchant_password or ""
    if not email or not password:
        return None
    if db.query(AppUser).filter(AppUser.role == "merchant").first():
        return None
    return create_merchant(db, email, password, settings.merchant_name or "Backstop Store").id


def create_merchant(db: Session, email: str, password: str, name: str) -> AppUser:
    if len(password) < 8:
        raise ValueError("The merchant password must be at least 8 characters")
    if db.query(AppUser).filter(AppUser.role == "merchant").first():
        raise ValueError("A merchant account already exists (this app has one merchant)")
    user = AppUser(email=email.strip().lower(), password_hash=hash_password(password), name=name, role="merchant")
    db.add(user)
    db.commit()
    write_audit_entry(db, "merchant_account_created", {"user_id": user.id})
    return user


def login_app_user(db: Session, email: str, password: str) -> AppUser | None:
    user = db.query(AppUser).filter(AppUser.email == (email or "").strip().lower()).first()
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def account_out(db: Session, user: AppUser) -> dict:
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == user.business_account_id).first() \
        if user.business_account_id else None
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role,
            "business_account_id": user.business_account_id,
            "company_name": acct.company_name if acct else None}


# ── Permission wording (fixed text, keyed on what the person allowed) ────────

def permission_notes(cp, *, autopay_mode: bool, test_mode: bool = False) -> list[dict]:
    """What the person can expect, given the permissions they have set. Fixed
    sentences chosen by code - no model text. `tone`: ok | info | warn."""
    out: list[dict] = []
    c = contacts.contact_out(cp)
    if not c["has_phone"]:
        out.append({"tone": "warn", "text": "We have no phone number for you. Add and verify one to get help by call or text if a payment goes wrong."})
        return out
    if not c["phone_verified"]:
        out.append({"tone": "warn", "text": f"Your number {c['phone_masked']} is not verified yet. Until it is, we will not call you."})
    lang = LANGUAGE_LABEL.get(c["preferred_language"], c["preferred_language"])
    out.append({"tone": "info", "text": f"Texts and calls are in {lang}."})
    if c["can_call"]:
        out.append({"tone": "ok", "text": (
            "You allowed calls. " + ("If a debit fails we text you first, then call only between 08:00 and 19:00, "
                                     "at most once a day, to help you pay or pick a date."
                                     if autopay_mode else
                                     "If an invoice is overdue we remind you by text first, then call only between "
                                     "08:00 and 19:00, at most once a day."))})
    else:
        out.append({"tone": "info", "text": (
            "You have not allowed calls, so we will only text you. " +
            ("If a debit fails you will get a text with a link to pay or choose a date."
             if autopay_mode else "Overdue invoices come to you as texts with a pay link."))})
    if c["memory_consent_current"]:
        out.append({"tone": "ok", "text": "You allowed us to remember our calls (for example a date you promised). Turn this off any time and we delete it."})
    else:
        out.append({"tone": "info", "text": "We do not keep notes from calls with you."})
    if autopay_mode:
        out.append({"tone": "info", "text": "Before any retry of a failed debit we text you at least 24 hours ahead. You can cancel autopay here at any time."})
        if test_mode:
            out.append({"tone": "warn", "text": "Razorpay Test Mode: no real money moves, no bank pre-debit message is sent, and a debit's result arrives about 5 minutes after it is made."})
    return out


class PermissionsIn(BaseModel):
    preferred_language: str | None = None
    voice_consent: bool | None = None
    memory_consent: bool | None = None


def _apply_permissions(db: Session, cp, req: PermissionsIn, via: str) -> None:
    if cp is None:
        raise HTTPException(409, detail={"code": "no_phone_number", "message": "Add and verify a phone number first."})
    if req.preferred_language is not None:
        if req.preferred_language not in contacts.VALID_LANGUAGES:
            raise HTTPException(422, "preferred_language must be one of en, hi, hinglish")
        cp.preferred_language = req.preferred_language
        db.commit()
    try:
        if req.voice_consent is not None:
            contacts.record_consent(db, cp, req.voice_consent, via=via)
        if req.memory_consent is not None:
            contacts.record_memory_consent(db, cp, req.memory_consent, via=via)
    except OtpError as e:
        raise HTTPException(e.status, detail={"code": e.code, "message": e.message})


def _consent_texts() -> dict:
    return {"voice": contacts.consent_text(), "memory": {
        "version": contacts.MEMORY_CONSENT_TEXT_VERSION, "text": contacts.MEMORY_CONSENT_TEXT}}


# ── Customer: autopay ────────────────────────────────────────────────────────

def _own_mandate(db: Session, customer: Customer, mandate_id: str) -> Mandate:
    m = db.query(Mandate).filter(Mandate.id == mandate_id, Mandate.customer_id == customer.id).first()
    if m is None:
        raise HTTPException(404, "Autopay not found")     # same answer for someone else's id
    return m


_HISTORY_TYPES = ("mandate_debit_captured", "mandate_debit_attempted", "mandate_debit_refused")


def _history(db: Session, m: Mandate) -> list[dict]:
    rows = (db.query(AuditLog).filter(AuditLog.action_type.in_(_HISTORY_TYPES), AuditLog.details.like(f"%{m.id}%"))
            .order_by(AuditLog.sequence_num.desc()).limit(30).all())
    out = []
    for r in rows:
        try:
            d = json.loads(r.details)
        except ValueError:
            continue
        if d.get("mandate_id") != m.id:
            continue
        status = {"mandate_debit_captured": "paid", "mandate_debit_attempted": "in progress",
                  "mandate_debit_refused": "refused"}[r.action_type]
        out.append({"at": r.created_at, "status": status, "amount_inr": d.get("amount_inr")})
    return out


def _queued(db: Session, m: Mandate) -> dict:
    """The next debit's two jobs: the notice-creating one and the debit itself."""
    rows = (db.query(ScheduledAction)
            .filter(ScheduledAction.status == ScheduledStatus.SCHEDULED.value,
                    ScheduledAction.kind.in_([ScheduledKind.MANDATE_PDN.value, ScheduledKind.MANDATE_DEBIT.value]),
                    ScheduledAction.subject_id == m.id)
            .order_by(ScheduledAction.due_at.asc()).all())
    pdn = next((r for r in rows if r.kind == ScheduledKind.MANDATE_PDN.value), None)
    debit = next((r for r in rows if r.kind == ScheduledKind.MANDATE_DEBIT.value), None)
    debit_at = None
    if debit is not None:
        debit_at = debit.due_at
    elif pdn is not None:
        try:
            debit_at = datetime.fromisoformat(json.loads(pdn.payload_json).get("debit_at"))
        except (TypeError, ValueError):
            debit_at = None
    return {"notice_at": pdn.due_at if pdn else None, "debit_at": debit_at}


def autopay_view(db: Session, customer: Customer, m: Mandate) -> dict:
    cp = contacts.for_customer(db, customer.id)
    q = _queued(db, m) if m.status == MandateState.ACTIVE.value else {"notice_at": None, "debit_at": None}
    return {
        "id": m.id, "status": m.status, "service_key": m.service_key, "service_name": m.service_name or "Autopay",
        "amount_inr": m.amount_inr, "frequency": m.frequency,
        "registration_url": m.registration_url if m.status == MandateState.SETUP.value else None,
        "next_debit_at": q["debit_at"] or (m.next_due_at if m.status == MandateState.ACTIVE.value else None),
        "next_debit_amount_inr": m.amount_inr if m.status == MandateState.ACTIVE.value else None,
        "notice_at": q["notice_at"], "history": _history(db, m), "created_at": m.created_at,
        "contact": contacts.contact_out(cp),
        "notes": permission_notes(cp, autopay_mode=True, test_mode=rz.is_test_key()) if m.status == MandateState.ACTIVE.value else [],
    }


class StartAutopayIn(BaseModel):
    service_key: str
    amount_inr: float | None = None
    frequency: str | None = None


@router.get("/me/autopay/services")
def autopay_services_list():
    return {"services": autopay_services.SERVICES,
            "custom": {"key": autopay_services.CUSTOM_KEY, "min_inr": autopay_services.CUSTOM_MIN_INR,
                       "max_inr": autopay_services.CUSTOM_MAX_INR, "frequencies": list(autopay_services.FREQUENCIES)},
            "test_mode": rz.is_test_key(), "consent_text": _consent_texts()}


@router.get("/me/autopay")
def my_autopay(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    rows = (db.query(Mandate).filter(Mandate.customer_id == customer.id)
            .order_by(Mandate.created_at.desc()).limit(20).all())
    return [autopay_view(db, customer, m) for m in rows]


@router.post("/me/autopay")
def start_autopay(req: StartAutopayIn, customer: Customer = Depends(get_current_customer),
                  db: Session = Depends(get_db)):
    try:
        svc = autopay_services.resolve(req.service_key, req.amount_inr, req.frequency)
    except autopay_services.ServiceError as e:
        raise HTTPException(422, str(e))
    if not rz.keys_present():
        raise HTTPException(503, "Razorpay keys are not configured on this server")
    # A setup the customer has not finished for the same thing: hand back its link.
    existing = (db.query(Mandate).filter(
        Mandate.customer_id == customer.id, Mandate.status == MandateState.SETUP.value,
        Mandate.service_key == svc["key"], Mandate.amount_inr == svc["amount_inr"],
        Mandate.frequency == svc["frequency"]).order_by(Mandate.created_at.desc()).first())
    if existing is not None and existing.registration_url:
        return autopay_view(db, customer, existing)
    cp = contacts.for_customer(db, customer.id)
    try:
        m = autopay.create_mandate(
            db, name=customer.name, phone=cp.phone if cp and cp.phone else None, email=customer.email,
            amount_inr=svc["amount_inr"], frequency=svc["frequency"], customer_id=customer.id,
            service_key=svc["key"], service_name=svc["name"])
    except rz.RazorpayError as e:
        raise HTTPException(502, f"Razorpay refused: {e.code} {e.description}")
    write_audit_entry(db, "customer_autopay_started", {
        "customer_id": customer.id, "mandate_id": m.id, "service": svc["key"], "amount_inr": svc["amount_inr"]})
    return autopay_view(db, customer, m)


@router.post("/me/autopay/{mandate_id}/sync")
def sync_autopay(mandate_id: str, customer: Customer = Depends(get_current_customer),
                 db: Session = Depends(get_db)):
    m = _own_mandate(db, customer, mandate_id)
    try:
        autopay.sync_mandate(db, m)
    except rz.RazorpayError as e:
        raise HTTPException(502, f"Razorpay refused: {e.code} {e.description}")
    db.refresh(m)
    return autopay_view(db, customer, m)


@router.post("/me/autopay/{mandate_id}/cancel")
def cancel_autopay(mandate_id: str, customer: Customer = Depends(get_current_customer),
                   db: Session = Depends(get_db)):
    m = _own_mandate(db, customer, mandate_id)
    if m.status == MandateState.CANCELLED.value:
        return autopay_view(db, customer, m)
    autopay.cancel_mandate(db, m, "customer")
    return autopay_view(db, customer, m)


@router.put("/me/autopay/permissions")
def autopay_permissions(req: PermissionsIn, customer: Customer = Depends(get_current_customer),
                        db: Session = Depends(get_db)):
    cp = contacts.for_customer(db, customer.id)
    _apply_permissions(db, cp, req, via="autopay_page")
    cp = contacts.for_customer(db, customer.id)
    return {"contact": contacts.contact_out(cp),
            "notes": permission_notes(cp, autopay_mode=True, test_mode=rz.is_test_key())}


# ── Merchant: business customers and invoices ────────────────────────────────

class BusinessCustomerIn(BaseModel):
    company_name: str
    contact_name: str | None = None
    email: str
    phone: str | None = None
    preferred_language: str = "hinglish"
    gstin: str | None = None
    payment_terms_days: int = 30


@router.post("/merchant/business-customers", dependencies=[Depends(require_merchant)])
def create_business_customer(req: BusinessCustomerIn, db: Session = Depends(get_db)):
    """Register a business AND its login. The one-time password is returned here
    and never stored in the clear: the merchant hands it over."""
    from app import receivables
    email = req.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "A valid email is needed for the business's login")
    if db.query(AppUser).filter(AppUser.email == email).first() or db.query(Customer).filter(Customer.email == email).first():
        raise HTTPException(409, "That email already has an account")
    if db.query(BusinessAccount).filter(BusinessAccount.email == email).first():
        raise HTTPException(409, "A business with that email already exists - edit it instead of adding it again")
    acct_out = receivables.create_account(receivables.AccountIn(
        company_name=req.company_name, gstin=req.gstin, contact_name=req.contact_name, email=email,
        phone=req.phone, preferred_language=req.preferred_language, payment_terms_days=req.payment_terms_days), db)
    password = secrets.token_urlsafe(9)
    user = AppUser(email=email, password_hash=hash_password(password), name=req.contact_name or req.company_name,
                   role="business", business_account_id=acct_out["id"])
    db.add(user)
    db.commit()
    write_audit_entry(db, "business_login_created", {"account_id": acct_out["id"], "user_id": user.id})
    return {"account_id": acct_out["id"], "company_name": req.company_name, "login_email": email,
            "temporary_password": password}


@router.get("/merchant/business-customers", dependencies=[Depends(require_merchant)])
def list_business_customers(db: Session = Depends(get_db)):
    logins = {u.business_account_id: u.email for u in db.query(AppUser).filter(AppUser.role == "business").all()}
    out = []
    for a in db.query(BusinessAccount).order_by(BusinessAccount.created_at.desc()).all():
        c = contacts.contact_out(contacts.for_business_account(db, a.id))
        out.append({"id": a.id, "company_name": a.company_name, "contact_name": a.contact_name, "email": a.email,
                    "gstin": a.gstin, "payment_terms_days": a.payment_terms_days,
                    "phone": a.phone,                      # the merchant typed it; shown so it can be corrected
                    "login_email": logins.get(a.id), "phone_masked": c["phone_masked"],
                    "phone_verified": c["phone_verified"], "can_call": c["can_call"],
                    "preferred_language": c["preferred_language"]})
    return out


class BusinessCustomerPatch(BaseModel):
    company_name: str | None = None
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    preferred_language: str | None = None
    gstin: str | None = None
    payment_terms_days: int | None = None


@router.put("/merchant/business-customers/{account_id}", dependencies=[Depends(require_merchant)])
def update_business_customer(account_id: str, req: BusinessCustomerPatch, db: Session = Depends(get_db)):
    """Correct a business customer's details. The merchant owns what it typed
    (name, email, number, language) - never the customer's CHOICES: consent is
    not editable here. A changed number clears that business's verification and
    consent (contacts.upsert_contact), so the business must verify and agree again."""
    try:
        uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(404, "Unknown business")
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == account_id).first()
    if acct is None:
        raise HTTPException(404, "Unknown business")
    changed: list[str] = []
    if req.company_name is not None:
        if not req.company_name.strip():
            raise HTTPException(422, "The company name cannot be empty")
        acct.company_name = req.company_name.strip()
        changed.append("company_name")
    for field in ("contact_name", "gstin"):
        v = getattr(req, field)
        if v is not None:
            setattr(acct, field, v.strip() or None)
            changed.append(field)
    if req.payment_terms_days is not None:
        if not 0 <= req.payment_terms_days <= 365:
            raise HTTPException(422, "Payment terms must be between 0 and 365 days")
        acct.payment_terms_days = req.payment_terms_days
        changed.append("payment_terms_days")
    if req.email is not None:
        email = req.email.strip().lower()
        if "@" not in email:
            raise HTTPException(422, "A valid email is needed")
        user = db.query(AppUser).filter(AppUser.role == "business", AppUser.business_account_id == acct.id).first()
        clash = db.query(AppUser).filter(AppUser.email == email, AppUser.id != (user.id if user else None)).first() \
            or db.query(Customer).filter(Customer.email == email).first()
        if clash is not None:
            raise HTTPException(409, "That email already has an account")
        acct.email = email
        if user is not None:
            user.email = email                       # the login follows the email
        changed.append("email")
    language = req.preferred_language
    if language is not None and language not in contacts.VALID_LANGUAGES:
        raise HTTPException(422, "preferred_language must be one of en, hi, hinglish")
    phone_cleared = False
    if req.phone is not None or language is not None:
        try:
            new_phone = otp_service.normalize_phone(req.phone) if req.phone else None
        except OtpError as e:
            raise HTTPException(422, detail={"code": e.code, "message": e.message})
        existing = contacts.for_business_account(db, acct.id)
        if new_phone and (existing is None or existing.phone != new_phone):
            phone_cleared = bool(existing and existing.phone)
            acct.phone = new_phone
            changed.append("phone")
        if language is not None:
            acct.preferred_language = language
            changed.append("preferred_language")
        if new_phone or language is not None:
            contacts.upsert_contact(db, business_account_id=acct.id, phone=new_phone, language=language)
    db.commit()
    write_audit_entry(db, "business_customer_updated", {"account_id": acct.id, "fields": changed,
                                                         "verification_and_consent_cleared": phone_cleared})
    return {"id": acct.id, "updated": changed, "verification_and_consent_cleared": phone_cleared}


@router.post("/merchant/business-customers/{account_id}/reset-password", dependencies=[Depends(require_merchant)])
def reset_business_password(account_id: str, db: Session = Depends(get_db)):
    """The one-time password is shown only once; if the business lost it the
    merchant issues another. Returned once, stored only as a hash."""
    user = db.query(AppUser).filter(AppUser.role == "business", AppUser.business_account_id == account_id).first()
    if user is None:
        raise HTTPException(404, "This business has no login")
    password = secrets.token_urlsafe(9)
    user.password_hash = hash_password(password)
    db.commit()
    write_audit_entry(db, "business_password_reset", {"account_id": account_id, "user_id": user.id})
    return {"login_email": user.email, "temporary_password": password}


class RegisterInvoiceIn(BaseModel):
    account_id: str
    amount_inr: float
    due_at: datetime
    invoice_no: str | None = None


def _next_invoice_no(db: Session) -> str:
    n = db.query(Invoice).count() + 1
    while db.query(Invoice).filter(Invoice.invoice_no == f"INV-{n:04d}").first():
        n += 1
    return f"INV-{n:04d}"


@router.post("/merchant/invoices", dependencies=[Depends(require_merchant)])
def register_invoice(req: RegisterInvoiceIn, db: Session = Depends(get_db)):
    from app import receivables
    if req.amount_inr <= 0:
        raise HTTPException(422, "amount_inr must be positive")
    inv_no = (req.invoice_no or "").strip() or _next_invoice_no(db)
    due = req.due_at
    if due.tzinfo is not None:          # the browser sends "...Z"; the app stores naive UTC
        due = due.astimezone(timezone.utc).replace(tzinfo=None)
    out = receivables._create_one(db, receivables.InvoiceIn(
        account_id=req.account_id, invoice_no=inv_no, amount_inr=req.amount_inr, due_at=due))
    return receivables._inv_out(out)


# ── Business customer: their invoices and permissions ────────────────────────

_business = require_role("business")


def _own_invoice(db: Session, user: AppUser, invoice_id: str) -> Invoice:
    inv = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.account_id == user.business_account_id).first()
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return inv


@router.get("/biz/invoices")
def my_invoices(user: AppUser = Depends(_business), db: Session = Depends(get_db)):
    from app import receivables
    cp = contacts.for_business_account(db, user.business_account_id)
    rows = (db.query(Invoice).filter(Invoice.account_id == user.business_account_id)
            .order_by(Invoice.due_at.asc()).all())
    return {"account": account_out(db, user),
            "invoices": [receivables._inv_out(i) for i in rows],
            "contact": contacts.contact_out(cp),
            "notes": permission_notes(cp, autopay_mode=False),
            "consent_text": _consent_texts()}


@router.post("/biz/invoices/{invoice_id}/pay-link")
def invoice_pay_link(invoice_id: str, user: AppUser = Depends(_business), db: Session = Depends(get_db)):
    from app.receivables import invoice_link
    inv = _own_invoice(db, user, invoice_id)
    if inv.settled or inv.status == "disputed":
        raise HTTPException(409, "Nothing to pay on this invoice right now")
    from app import payment_links
    with payment_links.customer_action():
        link = invoice_link(db, inv)
    write_audit_entry(db, "business_pay_link_opened", {"invoice_id": inv.id, "payment_link_row": link.id,
                                                       "is_stub": link.is_stub})
    return {"url": link.short_url, "is_stub": link.is_stub,
            "amount_inr": link.amount_inr - (link.amount_paid_inr or 0)}


class PhoneIn(BaseModel):
    phone: str


class PhoneCodeIn(BaseModel):
    phone: str
    code: str


def _otp_http(e: OtpError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


@router.post("/biz/contact/otp/send")
def business_otp_send(req: PhoneIn, user: AppUser = Depends(_business), db: Session = Depends(get_db)):
    try:
        return otp_service.send_otp(db, otp_service.normalize_phone(req.phone), "profile")
    except OtpError as e:
        raise _otp_http(e)


@router.post("/biz/contact/verify")
def business_otp_verify(req: PhoneCodeIn, user: AppUser = Depends(_business), db: Session = Depends(get_db)):
    try:
        phone = otp_service.normalize_phone(req.phone)
        otp_service.verify_otp(db, phone, "profile", req.code)
    except OtpError as e:
        raise _otp_http(e)
    cp = contacts.upsert_contact(db, business_account_id=user.business_account_id, phone=phone, verified_now=True)
    if cp.phone_verified_at is None:
        cp.phone_verified_at = datetime.utcnow()
        db.commit()
    return {"contact": contacts.contact_out(cp), "notes": permission_notes(cp, autopay_mode=False)}


@router.put("/biz/contact")
def business_permissions(req: PermissionsIn, user: AppUser = Depends(_business), db: Session = Depends(get_db)):
    cp = contacts.for_business_account(db, user.business_account_id)
    _apply_permissions(db, cp, req, via="business_account_page")
    cp = contacts.for_business_account(db, user.business_account_id)
    return {"contact": contacts.contact_out(cp), "notes": permission_notes(cp, autopay_mode=False)}


# ── Who am I (any role) ──────────────────────────────────────────────────────

@router.get("/auth/whoami")
def whoami(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    from app.auth import bearer_claims
    claims = bearer_claims(authorization)
    if claims is None:
        raise HTTPException(401, "Sign in first")
    sid, role = claims
    if role == "customer":
        c = db.query(Customer).filter(Customer.id == sid).first()
        if c is None:
            raise HTTPException(401, "Account no longer exists")
        return {"role": "customer", "id": c.id, "email": c.email, "name": c.name}
    user = db.query(AppUser).filter(AppUser.id == sid, AppUser.role == role).first()
    if user is None:
        raise HTTPException(401, "Account no longer exists")
    return account_out(db, user)

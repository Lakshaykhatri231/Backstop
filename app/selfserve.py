"""
Customer self-serve page backend (no login - the signed link IS the
credential, scoped to exactly one debt).

One visit does everything the customer can do for themselves:
  1. verify their number by OTP (the merchant typed it; the customer proves it)
  2. say when they will pay (becomes a promise: pauses chasing, moves the retry)
  3. pick a language
  4. tick consent to be called - possible only after step 1, and only ever
     recorded as customer-originated (contacts.record_consent)

Works for debts a promise can attach to (mandate runs and invoices). Nothing
here ever sees or sets the merchant's view of consent; the merchant endpoints
in contacts.py cannot grant it.
"""
import hmac
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import clock, contacts, otp as otp_service, promises, runtime_flags
from app.audit import write_audit_entry
from app.database import get_db
from app.models import BusinessAccount, Invoice, MandateRun
from app.otp import OtpError

router = APIRouter()

SUPPORTED = ("mandate_run", "invoice")


def link_path(subject_type: str, subject_id: str) -> str:
    """Relative URL of the customer's page for one debt."""
    tok = promises.make_promise_token(subject_type, subject_id)
    return f"/pay-later?st={subject_type}&sid={subject_id}&t={tok}"


class SubjectRef(BaseModel):
    subject_type: str
    subject_id: str
    token: str


def _authorise(req: SubjectRef) -> None:
    if req.subject_type not in SUPPORTED:
        raise HTTPException(404, "Unknown link")
    expected = promises.make_promise_token(req.subject_type, req.subject_id)
    if not hmac.compare_digest(expected, req.token or ""):
        # Same message as unknown subject: a bad token reveals nothing.
        raise HTTPException(404, "Unknown link")


def _contact_for(db: Session, st: str, sid: str):
    if st == "mandate_run":
        run = db.query(MandateRun).filter(MandateRun.id == sid).first()
        return contacts.for_razorpay_customer(db, run.razorpay_customer_id) if run else None
    inv = db.query(Invoice).filter(Invoice.id == sid).first()
    return contacts.for_business_account(db, inv.account_id) if inv else None


def _display(db: Session, st: str, sid: str) -> dict:
    if st == "mandate_run":
        run = db.query(MandateRun).filter(MandateRun.id == sid).first()
        return {"label": "Recurring payment", "ref": run.subscription_id if run else ""}
    inv = db.query(Invoice).filter(Invoice.id == sid).first()
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first() if inv else None
    return {"label": f"Invoice {inv.invoice_no}" if inv else "Invoice",
            "ref": inv.invoice_no if inv else "", "merchant_for": acct.company_name if acct else ""}


def _context(db: Session, st: str, sid: str) -> dict:
    info = promises._SUBJECTS[st](db, sid)
    if info is None:
        raise HTTPException(404, "Unknown link")
    today = promises.ist_today()
    max_days = (runtime_flags.get_promise_max_days_b2b() if info["b2b"]
                else runtime_flags.get_promise_max_days_b2c())
    cp = _contact_for(db, st, sid)
    existing_open = db.query(promises.PaymentPromise).filter(
        promises.PaymentPromise.subject_type == st, promises.PaymentPromise.subject_id == sid,
        promises.PaymentPromise.status == "open").first()
    return {
        **_display(db, st, sid),
        "subject_type": st,
        "amount_inr": info["amount_inr"],
        "is_open": info["is_open"],
        "min_date": (today + timedelta(days=1)).isoformat(),
        "max_date": (today + timedelta(days=max_days)).isoformat(),
        "open_promise_date": existing_open.promised_date.isoformat() if existing_open else None,
        "contact": contacts.contact_out(cp),
        "consent_text": {"version": contacts.CONSENT_TEXT_VERSION, "text": contacts.CONSENT_TEXT},
        "memory_consent_text": contacts.MEMORY_CONSENT_TEXT,
    }


@router.get("/self-serve/context")
def get_context(subject_type: str, subject_id: str, token: str, db: Session = Depends(get_db)):
    _authorise(SubjectRef(subject_type=subject_type, subject_id=subject_id, token=token))
    return _context(db, subject_type, subject_id)


@router.post("/self-serve/otp/send")
def send_code(req: SubjectRef, db: Session = Depends(get_db)):
    """Send a code to the number the MERCHANT has on file. The customer never
    types a number here, so this cannot be used to text arbitrary numbers."""
    _authorise(req)
    cp = _contact_for(db, req.subject_type, req.subject_id)
    if cp is None or not cp.phone:
        raise HTTPException(409, detail={
            "code": "no_phone_number",
            "message": "We don't have a phone number on file for you. Please contact the merchant."})
    try:
        return otp_service.send_otp(db, cp.phone, "self_serve")
    except OtpError as e:
        raise HTTPException(e.status, detail={"code": e.code, "message": e.message})


class SubmitIn(SubjectRef):
    otp_code: str | None = None
    promised_date: str | None = None          # ISO date
    preferred_language: str | None = None
    voice_consent: bool | None = None
    memory_consent: bool | None = None


@router.post("/self-serve/submit")
def submit(req: SubmitIn, db: Session = Depends(get_db)):
    _authorise(req)
    st, sid = req.subject_type, req.subject_id
    cp = _contact_for(db, st, sid)
    out: dict = {"verified": False, "consent": False, "promise": None, "promise_error": None}

    # 1. verify the number (once). A verified number is not re-asked.
    if cp is not None and cp.phone and cp.phone_verified_at is None and req.otp_code:
        try:
            otp_service.verify_otp(db, cp.phone, "self_serve", req.otp_code)
            cp.phone_verified_at = clock.now()
            db.commit()
            write_audit_entry(db, "contact_verified_via_self_serve", {
                "contact_id": cp.id, "subject_type": st, "subject_id": sid})
        except OtpError as e:
            raise HTTPException(e.status, detail={"code": e.code, "message": e.message})
    out["verified"] = bool(cp and cp.phone_verified_at)

    # 2. language + 3. consent (consent needs a verified number - enforced in
    #    record_consent, and again here so the response says why)
    if cp is not None:
        if req.preferred_language is not None:
            contacts.upsert_contact(db, business_account_id=cp.business_account_id,
                                    razorpay_customer_id=cp.razorpay_customer_id,
                                    customer_id=cp.customer_id, language=req.preferred_language)
            db.refresh(cp)
        if req.voice_consent is not None:
            try:
                contacts.record_consent(db, cp, req.voice_consent, via="self_serve_link")
            except OtpError as e:
                out["consent_error"] = {"code": e.code, "message": e.message}
        out["consent"] = bool(cp.voice_consent)
        if req.memory_consent is not None:
            try:
                contacts.record_memory_consent(db, cp, req.memory_consent, via="self_serve_link")
            except OtpError as e:
                out["consent_error"] = {"code": e.code, "message": e.message}

    # 4. promise. A rejected date does not undo the consent/language above.
    if req.promised_date:
        try:
            pdate = datetime.strptime(req.promised_date, "%Y-%m-%d").date()
        except ValueError:
            out["promise_error"] = {"code": "bad_date", "reason": "Date must be YYYY-MM-DD."}
        else:
            promise, code, reason = promises.create_promise(db, st, sid, pdate, "self_serve_link")
            if promise is None:
                out["promise_error"] = {"code": code, "reason": reason}
            else:
                out["promise"] = {"id": promise.id, "promised_date": pdate.isoformat(),
                                  "amount_inr": promise.amount_inr}

    out["contact"] = contacts.contact_out(cp)
    return out


@router.post("/self-serve/pay-link")
def pay_link(req: SubjectRef, db: Session = Depends(get_db)):
    """"Pay now" on the customer's own page: the debt's Razorpay Payment Link,
    made the first time someone presses it (Test Mode caps links, so a page
    view alone must not spend one) and reused after that. For a mandate run,
    paying it settles the run and cancels the queued retry debit."""
    _authorise(req)
    st, sid = req.subject_type, req.subject_id
    if st == "invoice":
        from app.receivables import invoice_link
        inv = db.query(Invoice).filter(Invoice.id == sid).first()
        if inv is None or inv.settled or inv.status == "disputed":
            raise HTTPException(409, "Nothing to pay on this invoice right now")
        link = invoice_link(db, inv)
    else:
        from app.autopay import run_payment_link
        run = db.query(MandateRun).filter(MandateRun.id == sid).first()
        if run is None or run.risk_settled:
            raise HTTPException(409, "Nothing to pay on this right now")
        link = run_payment_link(db, run)
    write_audit_entry(db, "self_serve_pay_link_opened", {
        "subject_type": st, "subject_id": sid, "payment_link_row": link.id, "is_stub": link.is_stub})
    return {"url": link.short_url, "is_stub": link.is_stub, "amount_inr": link.amount_inr - (link.amount_paid_inr or 0)}

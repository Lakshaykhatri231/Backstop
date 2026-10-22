"""
Who gets which message about which debt.

app/messaging.py renders, stores and delivers; this module answers the
questions in front of it: which number (always via ContactPoint, the single
source of truth), what name and language, and which link - and it labels the
link honestly. A link that is only a stand-in (no real Razorpay payment link
or subscription URL exists for that subject yet) is stored as `placeholder`,
so the inbox can say so instead of passing it off as a working payment URL.

Transactional text about a debt the customer already has does not need the
voice-call consent - but it still goes only to the number on file, and a debt
with no number records `not_sent_no_contact` rather than pretending.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import contacts, messaging
from app.models import BusinessAccount, Customer, Invoice, MandateRun, Order, OutboundMessage

_IST = timedelta(hours=5, minutes=30)


def _date(dt: datetime | None) -> str:
    return (dt + _IST).strftime("%d %b") if dt else ""


def _lang(cp) -> str:
    return (cp.preferred_language if cp and cp.preferred_language else contacts.DEFAULT_LANGUAGE)


# ── Mandate runs ─────────────────────────────────────────────────────────────

def mandate_message(db: Session, run: MandateRun, template: str,
                    retry_at: datetime | None = None) -> OutboundMessage:
    """template: pre_debit_notice | payment_link | remandate_link"""
    from app.selfserve import link_path   # local: selfserve imports promises/contacts
    cp = contacts.for_razorpay_customer(db, run.razorpay_customer_id)
    if template == "pre_debit_notice":
        # The self-serve page is real: pick a date, confirm number, consent.
        link, kind = link_path("mandate_run", run.id), "self_serve"
    elif template == "payment_link" and cp is not None and cp.phone and not run.risk_settled:
        # "Pay it now instead of waiting for the retry": a real Payment Link
        # for the cycle; paying it settles the run and cancels the pending
        # debit (app/autopay.py), so the customer is never charged twice.
        from app.autopay import run_payment_link
        pl = run_payment_link(db, run)
        link, kind = pl.short_url, ("placeholder" if pl.is_stub else "razorpay_payment_link")
    elif template == "remandate_link" and run.mandate_id:
        # Card mandate we debit ourselves: a fresh Razorpay registration link.
        from app.autopay import remandate_url
        url = remandate_url(db, run)
        link, kind = (url, "razorpay_registration_link") if url else (f"/remandate/{run.id}", "placeholder")
    else:
        # A Razorpay Subscription run (simulator / Demo Console scenario): no
        # real subscription exists to re-mandate, and with no number on file
        # there is nobody to spend a Test Mode link on - a labelled stand-in.
        path = "pay" if template == "payment_link" else "remandate"
        link, kind = f"/{path}/{run.id}", "placeholder"
    variables = {"name": "Customer", "amount": run.amount_inr, "date": _date(retry_at)}
    return messaging.send_message(
        db, template, subject_type="mandate_run", subject_id=run.id,
        phone=cp.phone if cp else None, language=_lang(cp), variables=variables,
        link=link, link_kind=kind)


# ── Invoices ─────────────────────────────────────────────────────────────────

def _invoice_link(db: Session, inv: Invoice, template: str, has_phone: bool):
    """(url, link_kind). A real link is created only for a message that asks
    for payment AND has someone to go to (Test Mode caps links at ~30); the
    courtesy reminder before the due date only carries a link that already
    exists."""
    from app import payment_links
    from app.receivables import invoice_link
    if template == "invoice_courtesy" or not has_phone or inv.settled:
        link = payment_links.open_link(db, "invoice", inv.id)
    else:
        link = invoice_link(db, inv)
    if link is None:
        return None, None
    return link.short_url, ("placeholder" if link.is_stub else "razorpay_payment_link")


def invoice_message(db: Session, inv: Invoice, template: str,
                    outstanding: float | None = None) -> OutboundMessage:
    """template: invoice_courtesy | invoice_overdue | payment_link | promise_reminder"""
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
    cp = contacts.for_business_account(db, inv.account_id)
    link, kind = _invoice_link(db, inv, template, bool(cp and cp.phone))
    amount = outstanding if outstanding is not None else inv.amount_inr - (inv.amount_paid or 0)
    variables = {"name": (acct.contact_name or acct.company_name) if acct else "Customer",
                 "amount": amount, "ref": inv.invoice_no, "due": _date(inv.due_at),
                 "date": _date(inv.due_at)}
    return messaging.send_message(
        db, template, subject_type="invoice", subject_id=inv.id,
        phone=cp.phone if cp else None, language=_lang(cp), variables=variables,
        link=link, link_kind=kind)


# ── Failed storefront orders ─────────────────────────────────────────────────

def order_payment_link(db: Session, order: Order) -> OutboundMessage:
    """A real Payment Link for the exact failed basket (with the offer it was
    already priced at, if any) - see app/order_links.py."""
    from app.order_links import send_order_link
    return send_order_link(db, order)


# ── Promise reminders ────────────────────────────────────────────────────────

def promise_reminder(db: Session, promise) -> OutboundMessage | None:
    """"You said you would pay on <date>" for either kind of debt."""
    date_s = promise.promised_date.strftime("%d %b")
    if promise.subject_type == "invoice":
        inv = db.query(Invoice).filter(Invoice.id == promise.subject_id).first()
        if inv is None:
            return None
        acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
        cp = contacts.for_business_account(db, inv.account_id)
        link, kind = _invoice_link(db, inv, "promise_reminder", bool(cp and cp.phone))
        return messaging.send_message(
            db, "promise_reminder", subject_type="invoice", subject_id=inv.id,
            phone=cp.phone if cp else None, language=_lang(cp),
            variables={"name": (acct.contact_name or acct.company_name) if acct else "Customer",
                       "amount": promise.amount_inr, "date": date_s},
            link=link, link_kind=kind)
    if promise.subject_type == "mandate_run":
        from app.selfserve import link_path
        run = db.query(MandateRun).filter(MandateRun.id == promise.subject_id).first()
        if run is None:
            return None
        cp = contacts.for_razorpay_customer(db, run.razorpay_customer_id)
        return messaging.send_message(
            db, "promise_reminder", subject_type="mandate_run", subject_id=run.id,
            phone=cp.phone if cp else None, language=_lang(cp),
            variables={"name": "Customer", "amount": promise.amount_inr, "date": date_s},
            link=link_path("mandate_run", run.id), link_kind="self_serve")
    return None

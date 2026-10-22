"""
Payment Link registry: every Razorpay Payment Link a customer is handed goes
through here, whatever the debt (invoice, failed storefront order, failed
mandate run).

Why a registry instead of a link column per feature:
- Test Mode caps a business at ~30 links. So a link is created LAZILY (only
  when a message is about to carry it - an invoice due in a month uses no slot)
  and REUSED (one open link per debt; a resend or second reminder carries the
  same link). A new one is made only when the amount or the offer changed, or
  the old one expired, and the old one is then cancelled on Razorpay.
- At the cap, in stub mode, or on a Razorpay error the row is a labelled
  stand-in (`is_stub`, `plink_stub_...`) and that is audited - a chase message
  is never sent with no way to pay, and never with a fake link presented as real.
- One webhook entry point for payment_link.* events, dispatched to the subject
  through register_link_handler (same registry shape as promises.register_subject).
- Settling a debt any other way (paid elsewhere, written off, disputed)
  cancels its link via close_for_subject, so a written-off invoice can no
  longer be paid at the old amount.

Times here are REAL time (datetime.utcnow), not clock.now(): the link lives on
Razorpay, which knows nothing about the Demo Console's fast-forward.

Money: apply_payment books total_revenue once per payment for invoice and
mandate-run links (failed-order links book through record_capture_revenue in
their handler); each subject handler decides the rest (receivables_collected,
a recovered order, a recovered mandate run). `classify_payment` tells
webhook.py which payment.captured / payment.failed belong to a link, so they
are neither double-booked nor mistaken for a storefront failure.
"""
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app import razorpay_client as rz
from app.audit import write_audit_entry
from app.config import settings
from app.models import PaymentLink, PaymentLinkStatus

logger = logging.getLogger(__name__)

OPEN_STATUSES = (PaymentLinkStatus.CREATED.value, PaymentLinkStatus.PARTIALLY_PAID.value)
STUB_PREFIX = "plink_stub_"
# Razorpay: expire_by >= 15 min ahead, <= 6 months. A link this close to its
# expiry is replaced rather than handed out (it could die before it is opened).
_MIN_LIFE = timedelta(minutes=20)
_MAX_LIFE = timedelta(days=180)

# Seeds, evals and bulk CSV import wrap their work in force_stub() so they
# never spend Test Mode slots.
_force_stub: ContextVar[bool] = ContextVar("payment_links_force_stub", default=False)


@contextmanager
def force_stub():
    token = _force_stub.set(True)
    try:
        yield
    finally:
        _force_stub.reset(token)


_customer_action: ContextVar[bool] = ContextVar("payment_links_customer_action", default=False)


@contextmanager
def customer_action():
    """A real person pressing "Pay now" - the Demo Console's links switch is a
    simulation setting and must never break a customer-facing page."""
    token = _customer_action.set(True)
    try:
        yield
    finally:
        _customer_action.reset(token)


def mode() -> str:
    """"real" or "stub" for the next link created."""
    if _force_stub.get():
        return "stub"
    from app import runtime_flags
    if runtime_flags.get_sim_real_links() is False and not _customer_action.get():     # the Demo Console's "don't spend real links" switch
        return "stub"
    m = (settings.razorpay_links or "auto").lower()
    if m == "stub":
        return "stub"
    if m == "real":
        return "real"
    return "real" if rz.keys_present() else "stub"


# ── Subject handlers ─────────────────────────────────────────────────────────

# on_payment(db, link, amount_inr, payment_id) -> dict    (link paid / partly paid)
# on_closed(db, link, status) -> None                     (expired / cancelled on Razorpay)
_ON_PAYMENT: dict[str, Callable] = {}
_ON_CLOSED: dict[str, Callable] = {}


def register_link_handler(subject_type: str, on_payment: Callable, on_closed: Callable | None = None) -> None:
    _ON_PAYMENT[subject_type] = on_payment
    if on_closed:
        _ON_CLOSED[subject_type] = on_closed


# ── Lookup ───────────────────────────────────────────────────────────────────

def open_link(db: Session, subject_type: str, subject_id: str) -> PaymentLink | None:
    return (db.query(PaymentLink)
            .filter(PaymentLink.subject_type == subject_type, PaymentLink.subject_id == str(subject_id),
                    PaymentLink.status.in_(OPEN_STATUSES))
            .order_by(PaymentLink.created_at.desc()).first())


def latest_link(db: Session, subject_type: str, subject_id: str) -> PaymentLink | None:
    return (db.query(PaymentLink)
            .filter(PaymentLink.subject_type == subject_type, PaymentLink.subject_id == str(subject_id))
            .order_by(PaymentLink.created_at.desc()).first())


def _same_terms(link: PaymentLink, amount_inr: float, offer: dict | None) -> bool:
    # A part-paid link still asks for (amount - paid), so an invoice's
    # outstanding after a part-payment matches it and the link is reused.
    if abs((link.amount_inr - (link.amount_paid_inr or 0)) - amount_inr) > 0.01:
        return False
    return (link.offer_pct or None) == ((offer or {}).get("pct") or None)


# ── Create / reuse ───────────────────────────────────────────────────────────

def ensure_link(
    db: Session,
    subject_type: str,
    subject_id: str,
    amount_inr: float,
    *,
    expire_at: datetime,
    description: str,
    customer: dict | None = None,
    accept_partial: bool = False,
    offer: dict | None = None,
) -> PaymentLink:
    """The link to put in a message about this debt, reusing the open one when
    its terms still match. `offer` = {"pct", "original_amount_inr", "ends_at"}
    for a discount the cart ALREADY had; expire_at must already be capped at
    offer["ends_at"] by the caller (asserted here - a link never outlives it)."""
    now = datetime.utcnow()
    if offer and offer.get("ends_at") and expire_at > offer["ends_at"]:
        expire_at = offer["ends_at"]
    expire_at = min(max(expire_at, now + _MIN_LIFE), now + _MAX_LIFE)

    current = open_link(db, subject_type, subject_id)
    if current is not None:
        # A stand-in made because the console had real links switched off is not
        # reused once real links are allowed again (a limit/error stand-in is).
        placeholder_now_real = current.is_stub and current.stub_reason == "stub_mode" and mode() == "real"
        if _same_terms(current, amount_inr, offer) and current.expire_at > now + _MIN_LIFE and not placeholder_now_real:
            return current
        _close(db, current, PaymentLinkStatus.SUPERSEDED.value, reason="terms_changed_or_expiring")

    link = PaymentLink(
        subject_type=subject_type, subject_id=str(subject_id), amount_inr=round(amount_inr, 2),
        original_amount_inr=(offer or {}).get("original_amount_inr"),
        offer_pct=(offer or {}).get("pct"), offer_ends_at=(offer or {}).get("ends_at"),
        expire_at=expire_at, status=PaymentLinkStatus.CREATED.value,
    )
    db.add(link)
    db.commit()
    db.refresh(link)

    stub_reason = None
    if mode() == "stub":
        stub_reason = "stub_mode"
    elif rz.is_test_key() and (db.query(PaymentLink).filter(PaymentLink.is_stub.is_(False),
                                                          PaymentLink.razorpay_link_id.isnot(None))
                               .count() >= settings.razorpay_test_link_cap):
        # Our own count already says the Test Mode cap is spent (e.g. a big CSV
        # import whose overdue invoices all get chased): don't even ask.
        stub_reason = "limit_reached"
        write_audit_entry(db, "payment_link_limit_reached", {
            "payment_link_row": link.id, "subject_type": subject_type, "subject_id": str(subject_id),
            "razorpay_error": "local count reached RAZORPAY_TEST_LINK_CAP"})
    else:
        try:
            data = rz.create_payment_link(
                amount_inr=link.amount_inr, reference_id=link.id.replace("-", "")[:40],
                description=description, expire_at=expire_at, customer=customer,
                accept_partial=accept_partial,
                notes={"rr_kind": subject_type, "rr_subject_id": str(subject_id), "rr_link": link.id})
            link.razorpay_link_id, link.short_url = data.get("id"), data.get("short_url")
        except rz.LinkLimitReached as e:
            stub_reason = "limit_reached"
            write_audit_entry(db, "payment_link_limit_reached", {
                "payment_link_row": link.id, "subject_type": subject_type, "subject_id": str(subject_id),
                "razorpay_error": e.description[:200]})
        except rz.RazorpayError as e:
            stub_reason = "error"
            logger.warning("payment link creation failed for %s %s: %s", subject_type, subject_id, e)
            write_audit_entry(db, "payment_link_creation_failed", {
                "payment_link_row": link.id, "subject_type": subject_type, "subject_id": str(subject_id),
                "razorpay_status": e.status, "razorpay_code": e.code, "razorpay_error": e.description[:200]})
    if stub_reason:
        link.is_stub, link.stub_reason = True, stub_reason
        link.razorpay_link_id = f"{STUB_PREFIX}{link.id.replace('-', '')[:16]}"
        link.short_url = f"https://rzp.io/i/stub-{link.id[:8]}"
    db.commit()
    write_audit_entry(db, "payment_link_created", {
        "payment_link_row": link.id, "razorpay_link_id": link.razorpay_link_id,
        "subject_type": subject_type, "subject_id": str(subject_id), "amount_inr": link.amount_inr,
        "offer_pct": link.offer_pct, "expire_at": expire_at.isoformat(),
        "is_stub": link.is_stub, "stub_reason": stub_reason})
    return link


# ── Close ────────────────────────────────────────────────────────────────────

def _close(db: Session, link: PaymentLink, status: str, reason: str, remote: bool = True) -> bool:
    """Atomic: only an OPEN row moves, so two closers never both cancel."""
    moved = (db.query(PaymentLink)
             .filter(PaymentLink.id == link.id, PaymentLink.status.in_(OPEN_STATUSES))
             .update({"status": status, "closed_at": datetime.utcnow()}, synchronize_session=False))
    db.commit()
    if not moved:
        return False
    db.refresh(link)
    cancelled_remote, err = False, None
    if remote and not link.is_stub and link.razorpay_link_id:
        try:
            rz.cancel_payment_link(link.razorpay_link_id)
            cancelled_remote = True
        except rz.RazorpayError as e:
            # Already paid/expired on Razorpay, or unreachable. Our row is
            # closed either way; a late payment still arrives by webhook and is
            # handled (and flagged) by the subject.
            err = f"{e.code}: {e.description[:120]}"
    write_audit_entry(db, "payment_link_closed", {
        "payment_link_row": link.id, "razorpay_link_id": link.razorpay_link_id,
        "subject_type": link.subject_type, "subject_id": link.subject_id,
        "status": status, "reason": reason, "cancelled_on_razorpay": cancelled_remote, "error": err})
    return True


def close_for_subject(db: Session, subject_type: str, subject_id: str, reason: str) -> int:
    closed = 0
    for link in (db.query(PaymentLink)
                 .filter(PaymentLink.subject_type == subject_type, PaymentLink.subject_id == str(subject_id),
                         PaymentLink.status.in_(OPEN_STATUSES)).all()):
        if _close(db, link, PaymentLinkStatus.CANCELLED.value, reason=reason):
            closed += 1
    return closed


# ── Webhooks ─────────────────────────────────────────────────────────────────

def _record_payment_once(db: Session, link: PaymentLink, payment_id: str | None, amount_inr: float) -> bool:
    """Payment-level dedupe on the link row (row lock), independent of the
    webhook event-id dedupe: payment_link.paid and .partially_paid for the same
    payment must never apply it twice."""
    row = db.query(PaymentLink).filter(PaymentLink.id == link.id).with_for_update().first()
    seen = json.loads(row.paid_payment_ids or "[]")
    if payment_id and payment_id in seen:
        db.commit()
        return False
    if payment_id:
        seen.append(payment_id)
    row.paid_payment_ids = json.dumps(seen)
    row.amount_paid_inr = round((row.amount_paid_inr or 0) + amount_inr, 2)
    db.commit()
    db.refresh(link)
    return True


def apply_payment(db: Session, link: PaymentLink, amount_inr: float, payment_id: str | None,
                  source: str) -> dict:
    """Apply one payment on a link, EXACTLY once per payment id, whichever
    webhook brings it first: payment_link.paid/.partially_paid or the
    payment.captured Razorpay sends for the same payment (either may arrive
    first, or alone if a webhook event is not subscribed).

    total_revenue: invoice and mandate-run link money is booked here (the
    long-standing rule that capture books revenue - receivables.record_payment
    and mandate settling never do). A failed-order link books through
    revenue.record_capture_revenue in its handler instead, like a store
    checkout, so it is NOT booked here."""
    if not _record_payment_once(db, link, payment_id, amount_inr):
        return {"status": "duplicate_payment", "payment_link_row": link.id}
    paid_all = (link.amount_paid_inr or 0) >= link.amount_inr - 0.01
    new_status = PaymentLinkStatus.PAID.value if paid_all else PaymentLinkStatus.PARTIALLY_PAID.value
    was_open = link.status in OPEN_STATUSES
    if was_open:
        link.status = new_status
        if paid_all:
            link.closed_at = datetime.utcnow()
    db.commit()
    write_audit_entry(db, "payment_link_payment", {
        "payment_link_row": link.id, "razorpay_link_id": link.razorpay_link_id, "payment_id": payment_id,
        "amount_inr": amount_inr, "status": link.status, "was_open": was_open, "source": source,
        "subject_type": link.subject_type, "subject_id": link.subject_id})
    if link.subject_type != "order":
        from app.revenue import adjust
        adjust(db, "total_revenue", amount_inr, reason=f"payment_captured_{link.subject_type}",
               order_id=payment_id)
    handler = _ON_PAYMENT.get(link.subject_type)
    result = handler(db, link, amount_inr, payment_id) if handler else {}
    return {"status": "processed", "payment_link_row": link.id, **(result or {})}


def handle_event(db: Session, event_name: str, inner_payload: dict) -> dict:
    entity = inner_payload.get("payment_link", {}).get("entity", {})
    payment = inner_payload.get("payment", {}).get("entity", {})
    rz_id = entity.get("id")
    link = db.query(PaymentLink).filter(PaymentLink.razorpay_link_id == rz_id).first() if rz_id else None
    if link is None:
        return {"status": "unmatched", "payment_link_id": rz_id}

    if event_name in ("payment_link.paid", "payment_link.partially_paid"):
        # The specific payment's amount, not the link's running total.
        return apply_payment(db, link, (payment.get("amount") or 0) / 100.0, payment.get("id"),
                             source=event_name)

    if event_name in ("payment_link.expired", "payment_link.cancelled"):
        status = PaymentLinkStatus.EXPIRED.value if event_name.endswith("expired") \
            else PaymentLinkStatus.CANCELLED.value
        # remote=False: Razorpay already closed it. A link WE cancelled is
        # already closed here, so its echo webhook is a no-op.
        if _close(db, link, status, reason=f"razorpay_{status}", remote=False):
            handler = _ON_CLOSED.get(link.subject_type)
            if handler:
                handler(db, link, status)
            return {"status": "processed", "payment_link_row": link.id, "closed": status}
        return {"status": "already_closed", "payment_link_row": link.id}

    return {"status": "ignored"}


def extend_for_promise(db: Session, subject_type: str, subject_id: str, needed_until: datetime) -> dict:
    """A promise was booked: keep this debt's open link alive past the promised
    day (plus grace) so the SAME link - and the same Test Mode slot - can take
    the payment. Uses Razorpay's update call (same link id, nothing created).
    `needed_until` is REAL time (link times are real time, see the module
    docstring). Never past Razorpay's six months from the link's creation; a link
    that cannot be updated (expired / closed) is left for the next chase to replace."""
    link = open_link(db, subject_type, subject_id)
    if link is None:
        return {"extended": False, "reason": "no_open_link"}
    ceiling = (link.created_at or datetime.utcnow()) + _MAX_LIFE - timedelta(hours=1)
    target = min(needed_until, ceiling)
    if link.expire_at >= target:
        return {"extended": False, "reason": "already_long_enough", "expire_at": link.expire_at}
    old = link.expire_at
    if not link.is_stub and link.razorpay_link_id:
        try:
            rz.update_payment_link_expiry(link.razorpay_link_id, target)
        except rz.RazorpayError as e:
            write_audit_entry(db, "payment_link_extend_failed", {
                "payment_link_row": link.id, "razorpay_code": e.code, "razorpay_error": e.description[:200]})
            return {"extended": False, "reason": f"razorpay_refused:{e.code}"}
    link.expire_at = target
    db.commit()
    write_audit_entry(db, "payment_link_extended", {
        "payment_link_row": link.id, "razorpay_link_id": link.razorpay_link_id, "subject_type": subject_type,
        "subject_id": str(subject_id), "old_expire_at": old.isoformat(), "new_expire_at": target.isoformat(),
        "capped_at_six_months": needed_until > ceiling, "new_link_created": False})
    return {"extended": True, "expire_at": target, "capped": needed_until > ceiling}


def sync_link(db: Session, link: PaymentLink) -> dict:
    """Ask Razorpay what became of this link and apply it the way the webhooks
    would - for setups without a tunnel ("Check payment now"). Payment-id dedupe
    in apply_payment means a webhook that also arrives books nothing twice.
    The `payments` list on the fetched link is read defensively (its field
    names are from Razorpay's docs and not yet confirmed by a probe run)."""
    if link.is_stub or not link.razorpay_link_id:
        return {"checked": False, "note": "This is a simulated stand-in link; there is nothing on Razorpay to check."}
    try:
        entity = rz.fetch_payment_link(link.razorpay_link_id)
    except rz.RazorpayError as e:
        return {"checked": False, "note": f"Razorpay refused: {e.code} {e.description}"}
    applied, seen = [], 0
    for p in entity.get("payments") or []:
        pid = p.get("payment_id") or p.get("id")
        if not pid or (p.get("status") or "captured") not in ("captured", "paid", "authorized"):
            continue
        seen += 1
        out = apply_payment(db, link, (p.get("amount") or 0) / 100.0, pid, source="sync")
        if out.get("status") != "duplicate_payment":     # a subject handler may overwrite "status"
            applied.append(pid)
    status = entity.get("status")
    closed = None
    if status in ("expired", "cancelled") and link.status in (PaymentLinkStatus.CREATED.value,
                                                              PaymentLinkStatus.PARTIALLY_PAID.value):
        if _close(db, link, status, reason=f"razorpay_{status}", remote=False):
            handler = _ON_CLOSED.get(link.subject_type)
            if handler:
                handler(db, link, status)
            closed = status
    note = ""
    if status in ("paid", "partially_paid") and not seen:
        note = "Razorpay says the link is paid but did not list the payment; wait for the webhook."
    write_audit_entry(db, "payment_link_synced", {
        "payment_link_row": link.id, "razorpay_status": status, "payments_seen": seen,
        "applied": applied, "closed": closed})
    return {"checked": True, "razorpay_status": status, "payments_seen": seen, "applied": applied,
            "closed": closed, "note": note}


def classify_payment(db: Session, payment_entity: dict) -> tuple[str | None, PaymentLink | None]:
    """Which of OUR flows a payment.captured / payment.failed belongs to.
    Returns (rr_kind, link_row): rr_kind is "invoice" | "order" | "mandate_run"
    for a link payment, "mandate_debit" / "mandate_auth" for card-mandate
    payments (app/autopay.py), None for a storefront / unknown payment.

    Primary key: the `notes` we put on every link / debit order (Razorpay
    copies them onto the payment - checked by scripts/razorpay_probe.py link).
    Fallback: a link payment's description is "#<link id without plink_>"."""
    notes = payment_entity.get("notes")
    kind = notes.get("rr_kind") if isinstance(notes, dict) else None
    link = None
    if isinstance(notes, dict) and notes.get("rr_link"):
        link = db.query(PaymentLink).filter(PaymentLink.id == notes["rr_link"]).first()
    if link is None:
        desc = payment_entity.get("description") or ""
        if desc.startswith("#") and len(desc) > 1:
            link = db.query(PaymentLink).filter(PaymentLink.razorpay_link_id == f"plink_{desc[1:]}").first()
    if link is not None:
        return link.subject_type, link
    if kind is None:
        from app.autopay import classify as autopay_classify   # local: autopay imports this module
        kind = autopay_classify(db, payment_entity)
    return kind, None


# ── Demo Console counter ─────────────────────────────────────────────────────

_account_cache: dict = {"at": None, "value": None}


def usage(db: Session, include_account: bool = False) -> dict:
    created_here = db.query(PaymentLink).filter(PaymentLink.is_stub.is_(False)).count()
    out = {"mode": mode(), "test_mode": rz.is_test_key(), "cap": settings.razorpay_test_link_cap,
           "real_links_created_here": created_here,
           "stand_ins": db.query(PaymentLink).filter(PaymentLink.is_stub.is_(True)).count(),
           "limit_hits": db.query(PaymentLink).filter(PaymentLink.stub_reason == "limit_reached").count()}
    if include_account and rz.keys_present():
        now = datetime.utcnow()
        if _account_cache["at"] is None or now - _account_cache["at"] > timedelta(seconds=60):
            try:
                total, skip = 0, 0
                while True:
                    page = rz.list_payment_links(count=100, skip=skip)
                    total += len(page)
                    if len(page) < 100:
                        break
                    skip += 100
                _account_cache.update(at=now, value=total)
            except rz.RazorpayError:
                _account_cache.update(at=now, value=None)
        out["account_links_total"] = _account_cache["value"]
    return out


# ── API (merchant) ───────────────────────────────────────────────────────────

from fastapi import APIRouter, Depends, HTTPException   # noqa: E402

from app.database import get_db   # noqa: E402

router = APIRouter()


@router.get("/payment-links/usage")
def usage_endpoint(db: Session = Depends(get_db)):
    return usage(db, include_account=True)


@router.get("/payment-links")
def list_links(db: Session = Depends(get_db)):
    rows = db.query(PaymentLink).order_by(PaymentLink.created_at.desc()).limit(200).all()
    return [{"id": l.id, "razorpay_link_id": l.razorpay_link_id, "short_url": l.short_url,
             "subject_type": l.subject_type, "subject_id": l.subject_id, "amount_inr": l.amount_inr,
             "amount_paid_inr": l.amount_paid_inr, "offer_pct": l.offer_pct,
             "original_amount_inr": l.original_amount_inr, "expire_at": l.expire_at,
             "status": l.status, "is_stub": l.is_stub, "stub_reason": l.stub_reason,
             "created_at": l.created_at} for l in rows]


@router.get("/payment-links/duplicates")
def duplicates_endpoint(db: Session = Depends(get_db)):
    from app.order_links import duplicate_payments
    return duplicate_payments(db)


@router.post("/payment-links/{link_row}/refund/{payment_id}")
def refund_duplicate(link_row: str, payment_id: str, db: Session = Depends(get_db)):
    """A person refunds a basket that was paid twice (store + link). Only a
    payment listed by /payment-links/duplicates can be refunded here."""
    from app.order_links import duplicate_payments
    if not any(d["payment_link_row"] == link_row and d["payment_id"] == payment_id
               for d in duplicate_payments(db)):
        raise HTTPException(404, "not a refundable duplicate payment")
    link = db.query(PaymentLink).filter(PaymentLink.id == link_row).with_for_update().first()
    refunded = json.loads(link.refunded_payment_ids or "[]")
    if payment_id in refunded:
        db.commit()
        raise HTTPException(409, "already refunded")
    try:
        r = rz.refund_payment(payment_id, notes={"rr_reason": "duplicate_basket_payment"})
    except rz.RazorpayError as e:
        db.rollback()
        raise HTTPException(502, f"Razorpay refused: {e.code} {e.description}")
    link.refunded_payment_ids = json.dumps(refunded + [payment_id])
    db.commit()
    write_audit_entry(db, "duplicate_basket_payment_refunded", {
        "payment_link_row": link.id, "payment_id": payment_id, "refund_id": r.get("id"),
        "amount_inr": (r.get("amount") or 0) / 100.0})
    return {"refund_id": r.get("id"), "status": r.get("status")}

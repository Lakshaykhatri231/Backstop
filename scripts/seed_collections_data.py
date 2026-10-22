"""
Seeds settled history for the Collections analysis (failed recurring debits,
promises to pay, voice calls, paid B2B invoices) so the analysis has enough
resolved cases to clear its minimum sample and show real patterns instead of
"not enough data yet".

Writes directly to the DB (needs no running server). Safe to re-run: every row
carries a "seed" marker in its id/ref and the whole set is skipped if present.

Seeded history is SETTLED history - never open ledger state (CLAUDE.md): every
run/invoice/promise is created already resolved, and the ledger is only touched
through revenue.adjust() for the paid invoices so the receivables figures on the
dashboard agree with the rows. The shapes are deliberately skewed so the
patterns have something to say:
  - recurring debits: recovered at retries 1-2, essentially never from retry 3
  - consumer promises: far-out ones are broken far more often
  - several "broken" promises were in fact paid a day or two late
  - the 3rd voice call converts far worse than the 1st
  - B2B invoices mostly arrive before day ~30

Usage:  python scripts/seed_collections_data.py
"""
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
import app.main  # noqa: F401  (registers every model so create_all sees them)
from app.models import (
    BusinessAccount, ContactPoint, Invoice, InvoiceStatus, MandateRun, MandateStatus, PaymentLink, PaymentLinkStatus,
    PaymentPromise, PromiseStatus, ScheduledAction, ScheduledKind, ScheduledStatus, VoiceCall,
)
from app.revenue import adjust

random.seed(7)
SEED = "seed-coll"


def days_ago(n: float) -> datetime:
    return datetime.utcnow() - timedelta(days=n)


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    if db.query(MandateRun).filter(MandateRun.subscription_id.like(f"{SEED}%")).first():
        print("Collections seed data (part 1) already present.")
        _seed_business_promises(db)
        _seed_outcomes_links_calls(db)
        db.close()
        return

    # ── recurring debits: 40 settled runs (24 recovered, 16 lost) ───────────
    recovered_runs: list[MandateRun] = []
    for i in range(40):
        recovered = i < 24
        attempts = random.choice([1, 1, 2]) if recovered else random.choice([3, 4, 5])
        opened = days_ago(random.uniform(15, 80))
        take = timedelta(days=random.uniform(1, 6) if recovered else 11)
        run = MandateRun(
            subscription_id=f"{SEED}-sub-{i}", razorpay_customer_id=f"{SEED}-cust-{i}",
            amount_inr=random.choice([299, 499, 799, 999]),
            # a retry fixes "insufficient funds" (money arrives) but never an expired card or a cancelled mandate
            failure_reason=random.choice(["insufficient_funds"] * 4 + ["bank_decline"]) if recovered else
            random.choice(["insufficient_funds", "card_expired", "card_expired", "mandate_revoked", "bank_decline"]),
            attempts_made=attempts, risk_booked=False, risk_settled=True,
            status=MandateStatus.RECOVERED.value if recovered else MandateStatus.CANCELLED.value,
            opened_at=opened, closed_at=opened + take,
        )
        db.add(run)
        if recovered:
            recovered_runs.append(run)
    db.commit()

    # ── consumer promises: 30 resolved; far-out ones break much more often ──
    for i, run in enumerate(recovered_runs[:15]):
        _promise(db, "mandate_run", run.id, run.amount_inr, horizon=random.choice([2, 3, 5]),
                 kept=random.random() < 0.85, via=random.choice(["voice", "voice", "self_serve_link"]),
                 base=run.opened_at)
    for i in range(15):
        run = MandateRun(
            subscription_id=f"{SEED}-psub-{i}", razorpay_customer_id=f"{SEED}-pcust-{i}", amount_inr=499,
            failure_reason="insufficient_funds",
            attempts_made=1, risk_booked=False, risk_settled=True, status=MandateStatus.CANCELLED.value,
            opened_at=days_ago(30), closed_at=days_ago(10),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        kept = random.random() < 0.25
        late_paid = (not kept) and random.random() < 0.4      # "broken" but paid a day or two after
        if late_paid:
            run.status = MandateStatus.RECOVERED.value
            run.closed_at = run.opened_at + timedelta(days=16 + random.choice([1, 2]))
        _promise(db, "mandate_run", run.id, run.amount_inr, horizon=random.choice([16, 20, 25]),
                 kept=kept, via="merchant_manual", base=run.opened_at)
    db.commit()

    # ── B2B: one account with a verified+consented contact, 24 paid invoices ─
    acct = BusinessAccount(company_name="Seed Wholesale Co", contact_name="Accounts Team",
                           phone="+919000000099", preferred_language="hinglish", payment_terms_days=30)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    db.add(ContactPoint(business_account_id=acct.id, phone="+919000000099", preferred_language="hinglish",
                        phone_verified_at=datetime.utcnow(), voice_consent=True, consent_at=datetime.utcnow(),
                        consent_source="customer_ticked", consent_text_version="seed-demo"))
    db.commit()
    for i in range(24):
        amount = random.choice([12000, 25000, 48000, 90000])
        due = days_ago(random.uniform(60, 120))
        late = random.choice([3, 6, 9, 14, 20, 28, 35, 45, 70])
        inv = Invoice(
            account_id=acct.id, invoice_no=f"{SEED}-INV-{i}", amount_inr=amount, amount_paid=amount,
            issued_at=due - timedelta(days=30), due_at=due, status=InvoiceStatus.PAID.value,
            overdue_booked=True, overdue_booked_amount=amount, overdue_released=amount, settled=True,
            paid_at=due + timedelta(days=late),
        )
        db.add(inv)
        # keep the receivables ledger in step with the rows (collected only:
        # the overdue->collected move is already complete for seeded history)
        adjust(db, "receivables_collected", amount, reason="seed_collections_history",
               order_id=inv.invoice_no)
    db.commit()

    # ── voice: 3 calls per subject on 12 invoice-less subjects; call 3 is weak
    for s in range(12):
        sid = f"{SEED}-voice-{s}"
        for n, p_productive in enumerate((0.55, 0.30, 0.06), start=1):
            productive = random.random() < p_productive
            db.add(VoiceCall(
                subject_type="invoice", subject_id=sid, language="hinglish", script_text="seed",
                status="completed", transcript="seed", intent="PROMISE" if productive else "CALLBACK",
                intent_source="seed", routed_to="promise_created:seed" if productive else "callback_scheduled",
                created_at=days_ago(20 - n),
            ))
    db.commit()
    _seed_business_promises(db)
    _seed_outcomes_links_calls(db)
    db.close()
    print("Seeded: 40 recurring-debit runs, 30 promises, 24 paid B2B invoices, 36 voice calls, plus business-invoice promises.")
    print("Open the dashboard -> Audit Log -> Collections Analysis.")


def _seed_business_promises(db) -> None:
    """Promises on business invoices, so the three settings that need them clear the minimum sample:
      - business promise horizon (kept rate by how far ahead; longer ones break more)
      - promise grace days (many BROKEN promises were in fact paid a day or two after the date)
      - promise max broken (a debt's 2nd and 3rd promises are kept far less often than its 1st)
    Every invoice and promise is created already resolved (settled history, never open ledger state)."""
    marker = f"{SEED}-B-INV-"
    if db.query(Invoice).filter(Invoice.invoice_no.like(f"{marker}%")).first():
        print("Collections seed data (business promises) already present.")
        return
    rnd = random.Random(11)
    acct = BusinessAccount(company_name="Seed Retail Traders", contact_name="Accounts", phone="+919000000098",
                           preferred_language="hinglish", payment_terms_days=30)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    kept_by_horizon = {3: 0.8, 5: 0.7, 7: 0.65, 10: 0.45, 14: 0.35, 21: 0.2, 28: 0.12}
    for i in range(60):
        amount = rnd.choice([15000, 32000, 54000, 80000])
        due = days_ago(rnd.uniform(75, 115))
        inv = Invoice(account_id=acct.id, invoice_no=f"{marker}{i}", amount_inr=amount, amount_paid=0.0,
                      issued_at=due - timedelta(days=30), due_at=due, status=InvoiceStatus.OVERDUE.value,
                      overdue_booked=True, overdue_booked_amount=amount, settled=False)
        db.add(inv)
        db.commit()
        db.refresh(inv)
        promises = 3                                  # at most; a debt only gets another promise while the last one broke
        when, paid_at = due + timedelta(days=3), None
        for n in range(promises):
            horizon = rnd.choice(list(kept_by_horizon))
            # a debt's 2nd and 3rd promises are kept much less often than its first
            p_kept = kept_by_horizon[horizon] * (1.0, 0.55, 0.2)[n]
            kept = rnd.random() < p_kept
            date_ = when + timedelta(days=horizon)
            db.add(PaymentPromise(
                subject_type="invoice", subject_id=inv.id, amount_inr=amount, captured_via="voice",
                promised_date=date_.date(), status=PromiseStatus.KEPT.value if kept else PromiseStatus.BROKEN.value,
                amount_paid=amount if kept else 0.0, created_at=when, resolved_at=date_ + timedelta(days=1)))
            if kept:
                paid_at = date_
                break
            if rnd.random() < 0.3:                    # broken on the day, but paid a day or two after: the grace period was too tight
                paid_at = date_ + timedelta(days=rnd.choice([1, 2]))
                break
            if rnd.random() < 0.2:                    # the customer stops promising; nobody pays
                break
            when = date_ + timedelta(days=2)
        if paid_at is not None:
            inv.status, inv.paid_at, inv.amount_paid = InvoiceStatus.PAID.value, paid_at, amount
            inv.settled, inv.overdue_released = True, amount
            adjust(db, "receivables_collected", amount, reason="seed_collections_history", order_id=inv.invoice_no)
        db.commit()
    print("Seeded business promises: 60 invoices with 1-3 promises each.")


def _seed_outcomes_links_calls(db) -> None:
    """The context for the analysis's supporting metrics, so they show something on a fresh database:
      - invoices that were NEVER paid (still open, disputed, written off), so the picture is not only the ones that got paid
      - reminder steps that ran before each payment (which step is followed by payment)
      - real payment links, paid or expired
      - how calls end (hand-offs, no answers, links, callbacks)
    Settled history only: the receivables ledger is moved with the rows (overdue / written off), exactly once."""
    if db.query(PaymentLink).filter(PaymentLink.razorpay_link_id.like("plink_seed_%")).first():
        print("Collections seed data (outcomes, links, calls) already present.")
        return
    from app import runtime_flags
    from app.rules_engine import ESCALATE_STEP
    rnd = random.Random(5)
    acct = db.query(BusinessAccount).filter(BusinessAccount.company_name == "Seed Wholesale Co").first()
    if acct is None:
        acct = BusinessAccount(company_name="Seed Wholesale Co", contact_name="Accounts Team", phone="+919000000099",
                               preferred_language="hinglish", payment_terms_days=30)
        db.add(acct)
        db.commit()
        db.refresh(acct)

    # invoices never paid: 14 still open (some old), 4 disputed, 5 written off
    unpaid = [("open", rnd.uniform(15, 100)) for _ in range(14)] + [("disputed", rnd.uniform(25, 70)) for _ in range(4)] \
        + [("written_off", rnd.uniform(90, 130)) for _ in range(5)]
    for i, (kind, age) in enumerate(unpaid):
        amount = rnd.choice([12000, 25000, 48000, 90000])
        due = days_ago(age)
        inv = Invoice(account_id=acct.id, invoice_no=f"{SEED}-X-INV-{i}", amount_inr=amount, amount_paid=0.0,
                      issued_at=due - timedelta(days=30), due_at=due, overdue_booked=True, overdue_booked_amount=amount,
                      status={"open": InvoiceStatus.OVERDUE, "disputed": InvoiceStatus.DISPUTED, "written_off": InvoiceStatus.WRITTEN_OFF}[kind].value,
                      settled=kind == "written_off", overdue_released=amount if kind == "written_off" else 0.0)
        db.add(inv)
        # ledger in step with the row: overdue money sits in receivables_overdue; a write-off has already moved on
        adjust(db, "receivables_written_off" if kind == "written_off" else "receivables_overdue", amount,
               reason="seed_collections_history", order_id=inv.invoice_no)
        db.commit()
        db.refresh(inv)
        for step, off in enumerate(runtime_flags.get_receivables_chase_offsets()):   # the reminders that ran and brought nothing
            at = due + timedelta(days=off)
            if at < datetime.utcnow():
                _chase(db, inv, step, at)
        _link(db, "invoice", inv.id, amount, due + timedelta(days=1), None if kind != "open" else rnd.random() < 0.3)

    # reminder steps before each payment, and a link per paid invoice
    for inv in db.query(Invoice).filter(Invoice.invoice_no.like(f"{SEED}-%"), Invoice.status == InvoiceStatus.PAID.value).all():
        if db.query(ScheduledAction).filter(ScheduledAction.subject_id == inv.id).first():
            continue
        offs = runtime_flags.get_receivables_chase_offsets()
        for step, off in enumerate(offs):
            at = inv.due_at + timedelta(days=off)
            if inv.paid_at and at < inv.paid_at:
                _chase(db, inv, step, at)
        esc = inv.due_at + timedelta(days=runtime_flags.get_receivables_escalate_after_days())
        if inv.paid_at and esc < inv.paid_at:
            _chase(db, inv, ESCALATE_STEP, esc)
        if inv.paid_at and rnd.random() < 0.7:
            _link(db, "invoice", inv.id, inv.amount_inr, max(inv.due_at, inv.paid_at - timedelta(days=rnd.uniform(0.2, 6))), True,
                  paid_at=inv.paid_at)

    # a link for failed-debit runs: most recovered ones were paid through it quickly; lost ones' links expired
    for r in db.query(MandateRun).filter(MandateRun.subscription_id.like(f"{SEED}-%")).all():
        if r.status == MandateStatus.RECOVERED.value and rnd.random() < 0.7:
            created = r.opened_at + timedelta(hours=rnd.uniform(1, 30))
            _link(db, "mandate_run", r.id, r.amount_inr, created, True, paid_at=created + timedelta(hours=rnd.uniform(1, 40)))
        elif r.status != MandateStatus.RECOVERED.value and rnd.random() < 0.6:
            _link(db, "mandate_run", r.id, r.amount_inr, r.opened_at + timedelta(hours=rnd.uniform(1, 30)), False)

    # how calls end
    ends = (["escalated:unclear"] * 4 + ["escalated:question_out_of_scope"] * 2 + ["payment_link_sent"] * 4 + ["callback_scheduled"] * 3
            + ["no_answer"] * 5)
    for n, routed in enumerate(ends):
        db.add(VoiceCall(subject_type="invoice", subject_id=f"{SEED}-voice-x{n}", language="hinglish", script_text="seed",
                         status="no_answer" if routed == "no_answer" else "completed",
                         transcript="" if routed == "no_answer" else "seed", intent="UNCLEAR", intent_source="seed",
                         routed_to=routed, created_at=days_ago(rnd.uniform(1, 25))))
    db.commit()
    print("Seeded outcomes: 23 never-paid invoices, reminder history, payment links, 18 more call outcomes.")


def _chase(db, inv, step, at) -> None:
    db.add(ScheduledAction(kind=ScheduledKind.INVOICE_CHASE.value, subject_type="invoice", subject_id=inv.id,
                           customer_ref=inv.account_id, due_at=at, status=ScheduledStatus.DONE.value, attempt_no=step + 1,
                           payload_json=f'{{"step": {step}, "invoice_no": "{inv.invoice_no}", "seed": 1}}', result="seed",
                           executed_at=at))


def _link(db, subject_type, subject_id, amount, created, paid, paid_at=None) -> None:
    n = db.query(PaymentLink).filter(PaymentLink.razorpay_link_id.like("plink_seed_%")).count()
    link = PaymentLink(razorpay_link_id=f"plink_seed_{n}", short_url=f"https://rzp.io/i/seed{n}", subject_type=subject_type,
                       subject_id=subject_id, amount_inr=amount, expire_at=created + timedelta(days=14), is_stub=False,
                       created_at=created)
    if paid:
        link.status, link.amount_paid_inr = PaymentLinkStatus.PAID.value, amount
        link.closed_at = paid_at or created + timedelta(hours=random.uniform(2, 60))
    elif paid is False:
        link.status, link.closed_at = PaymentLinkStatus.EXPIRED.value, created + timedelta(days=14)
    else:
        link.status = PaymentLinkStatus.CREATED.value
    db.add(link)
    db.commit()


def _promise(db, subject_type, subject_id, amount, horizon, kept, via, base):
    created = base + timedelta(hours=2)
    db.add(PaymentPromise(
        subject_type=subject_type, subject_id=subject_id, amount_inr=amount, captured_via=via,
        promised_date=(created + timedelta(days=horizon)).date(),
        status=PromiseStatus.KEPT.value if kept else PromiseStatus.BROKEN.value,
        amount_paid=amount if kept else 0.0, created_at=created, resolved_at=created + timedelta(days=horizon + 1),
    ))


if __name__ == "__main__":
    seed()

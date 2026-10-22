"""The Collections analysis: every metric is computed from known rows, the context metrics say what they should (and do not fake a
pattern from too few cases), and the seed script leaves every setting with enough evidence to be recommended on."""
import importlib.util
import json
import uuid
from datetime import datetime, timedelta

from evals import harness
from evals.harness import ROOT, Recorder


def run(ctx):
    from app import collections_insights as ci
    from app.database import SessionLocal
    from app.models import (
        BusinessAccount, Invoice, InvoiceStatus, MandateRun, MandateStatus, PaymentLink, PaymentPromise, PromiseStatus,
        ScheduledAction, ScheduledKind, ScheduledStatus, VoiceCall,
    )
    R = Recorder("collections_analysis", "Each Collections metric is right on known data; patterns are only stated when a group has enough cases; "
                                         "the seed data gives every setting enough evidence.")
    harness.wipe()
    db = SessionLocal()
    now = datetime.utcnow()
    try:
        # ── why debits fail: an expired card never recovers, insufficient funds mostly does ──
        for i in range(6):
            db.add(MandateRun(subscription_id=f"fr-ex-{i}", razorpay_customer_id="c", amount_inr=500, failure_reason="card_expired", attempts_made=4,
                              risk_settled=True, risk_booked=False, status=MandateStatus.CANCELLED.value, opened_at=now - timedelta(days=20),
                              closed_at=now - timedelta(days=9)))
        for i in range(6):
            ok = i < 5
            db.add(MandateRun(subscription_id=f"fr-if-{i}", razorpay_customer_id="c", amount_inr=500, failure_reason="insufficient_funds", attempts_made=1,
                              risk_settled=True, risk_booked=False, status=(MandateStatus.RECOVERED if ok else MandateStatus.CANCELLED).value,
                              opened_at=now - timedelta(days=20), closed_at=now - timedelta(days=18) if ok else now - timedelta(days=9)))
        db.commit()
        fr = {g["reason"]: g for g in ci.mandate_failure_reasons(db, None)["by_reason"]}
        R.case("failure_reasons:recovery_per_reason", fr["card_expired"]["recovery_rate"] == 0.0 and fr["insufficient_funds"]["recovered"] == 5
               and fr["insufficient_funds"]["median_days_to_recover"] == 2.0 and fr["card_expired"]["lost_inr"] == 3000.0, f"{fr}", critical=True)

        # ── what became of every overdue invoice, including the never-paid ──
        acct = BusinessAccount(company_name="Eval Co", contact_name="x", phone="+919000000001", preferred_language="hinglish", payment_terms_days=30)
        db.add(acct)
        db.commit()
        db.refresh(acct)

        def inv(no, status, age, paid=0.0, paid_at=None, amount=1000.0):
            row = Invoice(account_id=acct.id, invoice_no=no, amount_inr=amount, amount_paid=paid, issued_at=now - timedelta(days=age + 30),
                          due_at=now - timedelta(days=age), status=status, overdue_booked=True, overdue_booked_amount=amount, paid_at=paid_at)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row
        paid_inv = inv("co-paid", InvoiceStatus.PAID.value, 40, paid=1000.0, paid_at=now - timedelta(days=30))
        inv("co-open-old", InvoiceStatus.OVERDUE.value, 70)
        inv("co-open-new", InvoiceStatus.OVERDUE.value, 10)
        inv("co-disp", InvoiceStatus.DISPUTED.value, 30)
        inv("co-wo", InvoiceStatus.WRITTEN_OFF.value, 100)
        ro = ci.receivables_outcomes(db, None)
        R.case("outcomes:every_overdue_invoice_counted", ro["n"] == 5 and ro["counts"] == {"paid": 1, "partially_paid": 0, "still_open": 2, "disputed": 1, "written_off": 1}
               and ro["still_open_by_days_overdue"]["61-90d"] == 1 and ro["still_open_by_days_overdue"]["0-30d"] == 1 and ro["paid_share"] == 0.2, f"{ro}", critical=True)
        R.case("outcomes:unpaid_invoices_visible_where_timing_sees_only_paid", ci.receivables_timing(db, None)["n"] == 1 and ro["n"] == 5,
               "the survivorship gap is gone: the timing figure must stay paid-only while outcomes count them all")

        # ── which reminder is followed by payment (last step before the payment, within 3 days) ──
        def chase(invoice, step, at):
            db.add(ScheduledAction(kind=ScheduledKind.INVOICE_CHASE.value, subject_type="invoice", subject_id=invoice.id, due_at=at,
                                   status=ScheduledStatus.DONE.value, payload_json=json.dumps({"step": step}), executed_at=at))
        chase(paid_inv, 0, now - timedelta(days=41))          # 11 days before payment: no credit
        chase(paid_inv, 1, now - timedelta(days=32))          # 2 days before payment: credited
        db.commit()
        cs = {s["step"]: s for s in ci.chase_step_effectiveness(db, None)["steps"]}
        R.case("chase:last_step_within_three_days_gets_the_credit", cs[1]["paid_within_3d"] == 1 and cs[0]["paid_within_3d"] == 0 and cs[0]["sent"] == 1,
               f"{cs}", critical=True)

        # ── payment links: real only, a replaced link never had its chance ──
        def link(st, status, stub=False, paid=0.0, hours=None, n=[0]):
            n[0] += 1
            created = now - timedelta(days=3)
            db.add(PaymentLink(razorpay_link_id=f"plink_ev_{n[0]}", subject_type=st, subject_id="x", amount_inr=500, expire_at=created + timedelta(days=7),
                               status=status, is_stub=stub, amount_paid_inr=paid, created_at=created,
                               closed_at=created + timedelta(hours=hours) if hours else None))
        link("invoice", "paid", paid=500, hours=10)
        link("invoice", "paid", paid=500, hours=30)
        link("invoice", "expired")
        link("invoice", "superseded")
        link("invoice", "paid", stub=True, paid=500, hours=1)
        db.commit()
        lk = {g["subject_type"]: g for g in ci.link_conversion(db, None)["by_subject"]}["invoice"]
        R.case("links:stand_ins_and_replaced_links_are_left_out", lk["links"] == 3 and lk["paid"] == 2 and lk["expired_unpaid"] == 1 and lk["median_hours_to_pay"] == 20.0
               and abs(lk["paid_rate"] - 0.667) < 0.001, f"{lk}", critical=True)

        # ── promises by source, how calls end ──
        for i in range(6):
            db.add(PaymentPromise(subject_type="invoice", subject_id=str(uuid.uuid4()), amount_inr=100, captured_via="voice", promised_date=(now + timedelta(days=1)).date(),
                                  status=PromiseStatus.KEPT.value if i < 5 else PromiseStatus.BROKEN.value, created_at=now - timedelta(days=5)))
            db.add(PaymentPromise(subject_type="invoice", subject_id=str(uuid.uuid4()), amount_inr=100, captured_via="merchant_manual", promised_date=(now + timedelta(days=1)).date(),
                                  status=PromiseStatus.KEPT.value if i < 2 else PromiseStatus.BROKEN.value, created_at=now - timedelta(days=5)))
        for routed, status in (("promise_created:x", "completed"), ("payment_link_sent", "completed"), ("callback_scheduled", "completed"),
                               ("escalated:unclear", "completed"), ("", "no_answer"), ("escalated:no_date", "completed")):
            db.add(VoiceCall(subject_type="invoice", subject_id="v", language="hinglish", script_text="x", status=status, routed_to=routed, created_at=now - timedelta(days=2)))
        db.commit()
        src = {g["source"]: g for g in ci.promises_by_source(db, None)["by_source"]}
        R.case("promises:kept_rate_by_source", abs(src["voice"]["kept_rate"] - 0.833) < 0.001 and abs(src["merchant_manual"]["kept_rate"] - 0.333) < 0.001, f"{src}", critical=True)
        vo = ci.voice_outcomes(db, None)
        R.case("voice:call_endings", vo["counts"] == {"promise": 1, "payment_link": 1, "callback": 1, "no_answer": 1, "handed_to_person": 2, "other": 0}, f"{vo['counts']}", critical=True)

        # ── trend: nothing to compare for all-time; a number each side otherwise ──
        R.case("trend:none_for_all_time", ci.trend(db, None) is None, "")
        tr = ci.trend(db, now - timedelta(days=30))
        R.case("trend:has_both_windows", tr is not None and set(tr) == {"mandate_recovery_rate", "promise_kept_rate", "voice_productive_rate"}
               and tr["promise_kept_rate"]["n_now"] == 12, f"{tr}")

        # ── patterns: said when a group has the cases, silent when it does not ──
        full = ci.build_analysis_input(db, "all")
        texts = " | ".join(p["text"] for p in full["patterns"])
        R.case("patterns:failure_reason_gap_is_stated", "card expired" in texts and "recovered 0%" in texts, texts[:240], critical=True)
        R.case("patterns:uses_the_wider_cap", ci.MAX_COLLECTIONS_PATTERNS > 6, "")
        db.query(MandateRun).delete()
        db.commit()
        for i in range(3):         # three runs of a reason: shown in the metric, never turned into a pattern
            db.add(MandateRun(subscription_id=f"few-{i}", razorpay_customer_id="c", amount_inr=500, failure_reason="card_expired", attempts_made=3, risk_settled=True,
                              risk_booked=False, status=MandateStatus.CANCELLED.value, opened_at=now - timedelta(days=9), closed_at=now - timedelta(days=2)))
            db.add(MandateRun(subscription_id=f"few2-{i}", razorpay_customer_id="c", amount_inr=500, failure_reason="insufficient_funds", attempts_made=1, risk_settled=True,
                              risk_booked=False, status=MandateStatus.RECOVERED.value, opened_at=now - timedelta(days=9), closed_at=now - timedelta(days=8)))
        db.commit()
        few = ci.build_analysis_input(db, "all")
        R.case("patterns:small_groups_make_no_pattern", not any("retrying them the same way" in p["text"] for p in few["patterns"]), "a pattern was drawn from 3 cases a side", critical=True)

        # ── the seed data clears the minimum sample for every setting, and fills the new metrics ──
        db.close()
        harness.wipe()
        spec = importlib.util.spec_from_file_location("seed_collections_data", ROOT / "scripts" / "seed_collections_data.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.seed()
        mod.seed()          # second run: nothing is added twice
        db = SessionLocal()
        a = ci.build_analysis_input(db, "all")
        short = [p for p, e in a["evidence"].items() if not e["sufficient"]]
        R.case("seed:every_setting_has_enough_evidence", not short, f"too few cases for {short}", critical=True)
        R.case("seed:new_metrics_are_filled", a["mandate_failure_reasons"]["n"] > 0 and a["receivables_outcomes"]["counts"]["still_open"] > 0
               and a["chase_step_effectiveness"]["n"] > 0 and a["link_conversion"]["n"] > 0 and a["voice_outcomes"]["counts"]["handed_to_person"] > 0, "a context metric is empty on seed data")
        n_links = db.query(PaymentLink).count()
        mod.seed()
        R.case("seed:rerun_adds_nothing", db.query(PaymentLink).count() == n_links, "the seed duplicated rows on a second run")
        return R.result
    finally:
        db.close()

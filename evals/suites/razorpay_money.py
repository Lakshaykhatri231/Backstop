"""Real Razorpay Test Mode paths (Payment Links + card mandates) against a fake Razorpay: links are lazy, reused, capped and closed, money books exactly once, and a debit never fires on a settled run."""
import calendar
import hashlib
import hmac
import json
import re
from datetime import date, datetime, timedelta

import httpx

from evals import fixtures as fx
from evals.harness import Recorder


class FakeRazorpay:
    """Just enough of api.razorpay.com/v1 for these paths, in memory."""

    def __init__(self, link_cap: int = 30):
        self.link_cap, self.links, self.orders, self.payments, self.refunds = link_cap, {}, {}, [], []
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}_fake{self._n:06d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if method == "POST" and path == "/v1/payment_links":
            if len(self.links) >= self.link_cap:
                return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR",
                                                           "description": "Payment link creation limit exceeded in test mode"}})
            lid = self._id("plink")
            self.links[lid] = {**body, "id": lid, "status": "created", "short_url": f"https://rzp.io/i/{lid[-6:]}"}
            return httpx.Response(200, json=self.links[lid])
        m = re.fullmatch(r"/v1/payment_links/([^/]+)", path)
        if m and method == "PATCH":
            link = self.links.get(m.group(1))
            if link is None or link["status"] not in ("created", "partially_paid"):
                return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR",
                                                           "description": "update can only be made in created or partially paid state"}})
            link["expire_by"] = body["expire_by"]
            return httpx.Response(200, json=link)
        m = re.fullmatch(r"/v1/payment_links/([^/]+)/cancel", path)
        if m and method == "POST":
            link = self.links.get(m.group(1))
            if link is None or link["status"] != "created":
                return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "cannot cancel"}})
            link["status"] = "cancelled"
            return httpx.Response(200, json=link)
        if method == "POST" and path == "/v1/orders":
            oid = self._id("order")
            self.orders[oid] = {**body, "id": oid}
            return httpx.Response(200, json=self.orders[oid])
        if method == "POST" and path == "/v1/payments/create/recurring":
            pid = self._id("pay")
            self.payments.append({**body, "id": pid})
            return httpx.Response(200, json={"razorpay_payment_id": pid})
        m = re.fullmatch(r"/v1/payments/([^/]+)/refund", path)
        if m and method == "POST":
            rid = self._id("rfnd")
            self.refunds.append(m.group(1))
            return httpx.Response(200, json={"id": rid, "status": "processed", "amount": body.get("amount", 0)})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "description": path}})

    def open_links(self):
        return [l for l in self.links.values() if l["status"] == "created"]


def _ledger(db):
    from app.revenue import as_dict, get_or_create_state
    db.expire_all()
    return as_dict(get_or_create_state(db))


def _webhook(client, event: str, payload: dict, event_id: str):
    from app.config import settings
    body = json.dumps({"event": event, "payload": payload}).encode()
    sig = hmac.new(settings.razorpay_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/razorpay", content=body,
                       headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id,
                                "Content-Type": "application/json"})


def _link_paid_payload(link_row, amount_inr, pid, rz_link_id=None):
    pay = {"id": pid, "amount": int(round(amount_inr * 100)), "status": "captured",
           "notes": {"rr_kind": link_row.subject_type, "rr_subject_id": link_row.subject_id, "rr_link": link_row.id}}
    return {"payment_link": {"entity": {"id": rz_link_id or link_row.razorpay_link_id}}, "payment": {"entity": pay}}


def run(ctx):
    from fastapi.testclient import TestClient
    from app import autopay, clock, mandate, notify, order_links, payment_links, promises
    from app import razorpay_client as rz, receivables, scheduler
    from app.config import settings
    from app.database import SessionLocal
    from app.main import app
    from app.models import (
        AuditLog, BusinessAccount, CartEvent, CartEventStatus, CartEventType, Customer, CustomerTier, Event,
        Mandate, MandateRun, MandateState, MandateStatus, Order, OrderStatus, PaymentLink, PaymentLinkStatus,
        ScheduledAction, ScheduledKind, ScheduledStatus,
    )
    from app.revenue import adjust
    from app.rules_engine import CARD_TOKEN_DEBIT_LEAD

    fx.patch_world()
    R = Recorder("razorpay_money", "Payment Links are created lazily, reused, capped and cancelled on settle; link and mandate money "
                                   "books exactly once; failed link attempts are not storefront failures; card-mandate debits follow "
                                   "our schedule and never fire on a settled run.")
    db = SessionLocal()
    fake = FakeRazorpay(link_cap=30)
    saved = (settings.razorpay_links, settings.razorpay_key_id, settings.razorpay_key_secret, settings.razorpay_webhook_secret)
    settings.razorpay_links, settings.razorpay_key_id, settings.razorpay_key_secret = "real", "rzp_test_eval", "eval_secret"
    settings.razorpay_webhook_secret = settings.razorpay_webhook_secret or "eval_webhook_secret"
    rz.set_transport(httpx.MockTransport(fake.handler))
    client = TestClient(app)
    audits = lambda t: db.query(AuditLog).filter(AuditLog.action_type == t).count()
    jobs = lambda st, sid, kind, status=ScheduledStatus.SCHEDULED.value: db.query(ScheduledAction).filter_by(
        subject_type=st, subject_id=str(sid), kind=kind.value, status=status).all()
    try:
        # ── Invoices: lazy, reused, part-paid once, closed on settle ────────────
        cp, inv = fx.invoice_customer(db, amount=6500)
        R.case("invoice:no_link_at_creation", db.query(PaymentLink).filter_by(subject_id=inv.id).count() == 0
               and not fake.links, f"links={len(fake.links)}", critical=True)
        m1 = notify.invoice_message(db, inv, "invoice_overdue")
        m2 = notify.invoice_message(db, inv, "invoice_overdue")
        links = db.query(PaymentLink).filter_by(subject_type="invoice", subject_id=inv.id).all()
        R.case("invoice:first_chase_creates_one_real_link", len(fake.links) == 1 and len(links) == 1 and not links[0].is_stub
               and m1.link_kind == "razorpay_payment_link", f"fake={len(fake.links)} rows={len(links)} kind={m1.link_kind}", critical=True)
        R.case("invoice:resend_reuses_link", m1.link == m2.link and len(fake.links) == 1, f"{m1.link} vs {m2.link}", critical=True)
        link = links[0]
        notes = fake.links[link.razorpay_link_id]["notes"]
        R.case("invoice:link_carries_notes_and_no_razorpay_sms", notes.get("rr_link") == link.id
               and fake.links[link.razorpay_link_id]["notify"] == {"sms": False, "email": False}, f"{notes}")

        before = _ledger(db)
        pay = _link_paid_payload(link, 2000, "pay_inv_part")
        _webhook(client, "payment_link.partially_paid", pay, "evt_inv_1")
        _webhook(client, "payment.captured", {"payment": pay["payment"]}, "evt_inv_2")
        after = _ledger(db)
        db.refresh(inv)
        R.case("invoice:part_payment_applied_once", abs(inv.amount_paid - 2000) < 0.01,
               f"amount_paid={inv.amount_paid}", critical=True)
        R.case("invoice:revenue_booked_once_across_two_webhooks",
               abs(after["total_revenue"] - before["total_revenue"] - 2000) < 0.01,
               f"total_revenue {before['total_revenue']} -> {after['total_revenue']}", critical=True)
        R.case("invoice:not_booked_as_unmatched", audits("payment_captured") == 0, "an unmatched capture audit was written")
        m3 = notify.invoice_message(db, inv, "invoice_overdue")
        R.case("invoice:part_paid_link_reused", m3.link == m1.link and len(fake.links) == 1, f"fake links={len(fake.links)}")

        receivables.credit_note(db, inv, 500, "eval")
        db.refresh(link)
        R.case("invoice:credit_note_cancels_link", link.status == PaymentLinkStatus.CANCELLED.value
               and fake.links[link.razorpay_link_id]["status"] == "cancelled", f"row={link.status}", critical=True)
        m4 = notify.invoice_message(db, inv, "invoice_overdue")
        new = payment_links.open_link(db, "invoice", inv.id)
        R.case("invoice:new_link_for_new_amount", new is not None and abs(new.amount_inr - 4000) < 0.01 and m4.link == new.short_url,
               f"new={new and new.amount_inr}")
        receivables.write_off(db, inv, "eval")
        db.refresh(new)
        R.case("invoice:write_off_cancels_link", new.status == PaymentLinkStatus.CANCELLED.value
               and fake.links[new.razorpay_link_id]["status"] == "cancelled", f"{new.status}", critical=True)

        acct = BusinessAccount(company_name="No Phone Co [eval]", phone=None)
        db.add(acct)
        db.commit()
        inv2 = receivables.create_invoice(db, acct, "EVAL-NOPHONE", 900, issued_at=clock.now() - timedelta(days=40),
                                          due_at=clock.now() - timedelta(days=10))
        n_before = len(fake.links)
        notify.invoice_message(db, inv2, "invoice_overdue")
        R.case("invoice:no_number_spends_no_slot", len(fake.links) == n_before, f"{n_before} -> {len(fake.links)}")

        # ── A promise keeps the SAME link alive: extended, never replaced ───────
        cpe, inve = fx.invoice_customer(db, amount=3000)
        notify.invoice_message(db, inve, "invoice_overdue")
        le = payment_links.open_link(db, "invoice", inve.id)
        le.expire_at = datetime.utcnow() + timedelta(days=2)       # shorter than the promise below
        db.commit()
        n_links, rz_id = len(fake.links), le.razorpay_link_id
        today_ = (clock.now() + timedelta(hours=5, minutes=30)).date()
        pe, _c, _m = promises.create_promise(db, "invoice", inve.id, today_ + timedelta(days=12), "merchant_manual")
        db.refresh(le)
        R.case("promise:link_extended_not_replaced", pe is not None and le.status == PaymentLinkStatus.CREATED.value
               and le.razorpay_link_id == rz_id and len(fake.links) == n_links
               and le.expire_at > datetime.utcnow() + timedelta(days=12), f"expire_at={le.expire_at} links {n_links}->{len(fake.links)}", critical=True)
        R.case("promise:razorpay_told_the_new_expiry", abs(fake.links[rz_id]["expire_by"] - calendar.timegm(le.expire_at.timetuple())) <= 2,
               f"razorpay={fake.links[rz_id].get('expire_by')} ours={calendar.timegm(le.expire_at.timetuple())}")
        R.case("promise:extension_audited_with_no_new_link", audits("payment_link_extended") >= 1, "no payment_link_extended audit")
        fake.links[rz_id]["status"] = "expired"
        le.status = PaymentLinkStatus.CREATED.value
        db.commit()
        out = payment_links.extend_for_promise(db, "invoice", inve.id, datetime.utcnow() + timedelta(days=100))
        R.case("promise:refused_extension_is_audited_not_fatal", out["extended"] is False and audits("payment_link_extend_failed") >= 1, f"{out}")
        fake.links[rz_id]["status"] = "created"
        far = payment_links.extend_for_promise(db, "invoice", inve.id, datetime.utcnow() + timedelta(days=900))
        db.refresh(le)
        R.case("promise:never_past_six_months_from_creation", far.get("extended") and le.expire_at < le.created_at + timedelta(days=180),
               f"{far} expire={le.expire_at} created={le.created_at}")

        # ── Cap: a labelled stand-in, audited, never a message with no link ─────
        fake.link_cap = len(fake.links)
        cp3, inv3 = fx.invoice_customer(db, amount=1200)
        m5 = notify.invoice_message(db, inv3, "invoice_overdue")
        row = payment_links.open_link(db, "invoice", inv3.id)
        R.case("cap:stand_in_labelled", row is not None and row.is_stub and row.stub_reason == "limit_reached"
               and m5.link_kind == "placeholder" and m5.link, f"row={row and (row.is_stub, row.stub_reason)} kind={m5.link_kind}", critical=True)
        R.case("cap:audited", audits("payment_link_limit_reached") >= 1, "no payment_link_limit_reached audit")
        fake.link_cap = 1000

        # ── Failed link attempt is not a storefront failure ─────────────────────
        events_before = db.query(Event).count()
        fail = {"payment": {"entity": {"id": "pay_link_fail", "amount": 120000, "status": "failed",
                                       "order_id": "order_not_local", "error_reason": "payment_failed",
                                       "notes": {"rr_kind": "invoice", "rr_link": row.id}}}}
        _webhook(client, "payment.failed", fail, "evt_fail_1")
        R.case("link_fail:no_event_or_decision", db.query(Event).count() == events_before,
               f"events {events_before} -> {db.query(Event).count()}", critical=True)

        # ── Failed storefront order: offer shown, link ends with the offer ──────
        cust = Customer(email=f"eval-shopper-{datetime.utcnow().timestamp()}@example.invalid", password_hash="x",
                        name="Eval Shopper", tier=CustomerTier.REGULAR)
        db.add(cust)
        db.commit()
        from app import contacts
        contacts.upsert_contact(db, customer_id=cust.id, phone="+919812300001")
        offer_end = datetime.utcnow() + timedelta(hours=10)
        ce = CartEvent(customer_id=cust.id, event_type=CartEventType.EXPLICIT_CANCEL, amount_inr=3999,
                       tier_at_time=CustomerTier.REGULAR, status=CartEventStatus.RESUMED, incentive_pct=10,
                       incentive_final_amount_inr=3599.1, expires_at=offer_end, items_json='[{"sku":"sku_002","qty":1}]')
        db.add(ce)
        db.commit()
        order = Order(customer_id=cust.id, razorpay_order_id="order_eval_failed1", items_json='[{"sku": "sku_002", "qty": 1}]',
                      amount_inr=3599.1, status=OrderStatus.FAILED, recovered_from_cart_event_id=ce.id)
        db.add(order)
        db.commit()
        adjust(db, "at_risk_failed", 3599.1, reason="eval_seed", order_id=order.razorpay_order_id)
        msg = notify.order_payment_link(db, order)
        olink = payment_links.open_link(db, "order", order.id)
        R.case("order:offer_message", msg.template == "payment_link_offer" and "10%" in msg.body and "3,999" in msg.body
               and "3,599" in msg.body, f"template={msg.template} body={msg.body!r}", critical=True)
        R.case("order:link_never_outlives_offer", olink is not None and olink.expire_at <= offer_end
               and abs(olink.amount_inr - 3599.1) < 0.01 and olink.offer_pct == 10, f"expire={olink and olink.expire_at} end={offer_end}", critical=True)

        led0 = _ledger(db)
        pay = _link_paid_payload(olink, 3599.1, "pay_order_link")
        _webhook(client, "payment.captured", {"payment": pay["payment"]}, "evt_ord_1")
        _webhook(client, "payment_link.paid", pay, "evt_ord_2")
        led1 = _ledger(db)
        db.refresh(order)
        R.case("order:link_payment_captures_order", order.status == OrderStatus.CAPTURED and order.risk_settled,
               f"status={order.status} settled={order.risk_settled}", critical=True)
        R.case("order:revenue_once_and_risk_released",
               abs(led1["total_revenue"] - led0["total_revenue"] - 3599.1) < 0.01
               and abs(led0["at_risk_failed"] - led1["at_risk_failed"] - 3599.1) < 0.01
               and abs(led1["total_recovered"] - led0["total_recovered"] - 3599.1) < 0.01,
               f"rev {led0['total_revenue']}->{led1['total_revenue']} risk {led0['at_risk_failed']}->{led1['at_risk_failed']}", critical=True)

        # offer ended -> back to the store, no link
        ce2 = CartEvent(customer_id=cust.id, event_type=CartEventType.EXPLICIT_CANCEL, amount_inr=1499,
                        tier_at_time=CustomerTier.REGULAR, status=CartEventStatus.RESUMED, incentive_pct=10,
                        incentive_final_amount_inr=1349.1, expires_at=datetime.utcnow() - timedelta(hours=1))
        db.add(ce2)
        db.commit()
        order2 = Order(customer_id=cust.id, razorpay_order_id="order_eval_failed2", items_json='[{"sku": "sku_001", "qty": 1}]',
                       amount_inr=1349.1, status=OrderStatus.FAILED, recovered_from_cart_event_id=ce2.id)
        db.add(order2)
        db.commit()
        n_before = len(fake.links)
        msg2 = notify.order_payment_link(db, order2)
        R.case("order:offer_ended_goes_to_store", msg2.template == "order_offer_ended" and msg2.link_kind == "storefront"
               and len(fake.links) == n_before, f"template={msg2.template} kind={msg2.link_kind}", critical=True)

        # store capture of the same basket cancels the link; a late link payment is flagged, not booked
        order3 = Order(customer_id=cust.id, razorpay_order_id="order_eval_failed3", items_json='[{"sku": "sku_004", "qty": 2}]',
                       amount_inr=1798, status=OrderStatus.FAILED)
        db.add(order3)
        db.commit()
        adjust(db, "at_risk_failed", 1798, reason="eval_seed", order_id=order3.razorpay_order_id)
        notify.order_payment_link(db, order3)
        l3 = payment_links.open_link(db, "order", order3.id)
        retry = Order(customer_id=cust.id, razorpay_order_id="order_eval_retry3", items_json='[{"sku": "sku_004", "qty": 2}]',
                      amount_inr=1798, status=OrderStatus.CAPTURED, razorpay_payment_id="pay_store_3")
        db.add(retry)
        db.commit()
        from app.revenue import record_capture_revenue
        record_capture_revenue(db, retry, was_recovery=False)
        db.refresh(l3)
        R.case("order:store_capture_cancels_link", l3.status == PaymentLinkStatus.CANCELLED.value
               and fake.links[l3.razorpay_link_id]["status"] == "cancelled", f"{l3.status}", critical=True)
        led2 = _ledger(db)
        _webhook(client, "payment_link.paid", _link_paid_payload(l3, 1798, "pay_late_dup"), "evt_dup_1")
        led3 = _ledger(db)
        dups = order_links.duplicate_payments(db)
        R.case("order:late_link_payment_flagged_not_booked",
               abs(led3["total_revenue"] - led2["total_revenue"]) < 0.01 and any(d["payment_id"] == "pay_late_dup" for d in dups),
               f"rev {led2['total_revenue']}->{led3['total_revenue']} dups={dups}", critical=True)
        r = client.post(f"/payment-links/{l3.id}/refund/pay_late_dup")
        r2 = client.post(f"/payment-links/{l3.id}/refund/pay_late_dup")
        R.case("order:duplicate_refunded_once", r.status_code == 200 and r2.status_code in (404, 409)
               and fake.refunds.count("pay_late_dup") == 1, f"{r.status_code} {r2.status_code} refunds={fake.refunds}")

        # one reminder on the named day (no promise row)
        order4 = Order(customer_id=cust.id, razorpay_order_id="order_eval_failed4", items_json='[{"sku": "sku_003", "qty": 1}]',
                       amount_inr=5999, status=OrderStatus.FAILED)
        db.add(order4)
        db.commit()
        named = (clock.now() + timedelta(hours=5, minutes=30)).date() + timedelta(days=2)
        order_links.schedule_reminder(db, order4, named)
        order_links.schedule_reminder(db, order4, named + timedelta(days=1))
        rem = jobs("order", order4.id, ScheduledKind.ORDER_LINK_REMINDER)
        from app.models import PaymentPromise
        R.case("order:one_reminder_no_promise", len(rem) == 1 and db.query(PaymentPromise).filter_by(subject_id=order4.id).count() == 0,
               f"reminders={len(rem)}", critical=True)

        # ── Card mandates: our schedule, real debit shape, never on a settled run ─
        m = Mandate(customer_name="Eval Autopay", contact="+919812300002", email="eval@example.invalid",
                    razorpay_customer_id="cust_eval_m1", token_id="token_eval_1", status=MandateState.ACTIVE.value,
                    amount_inr=499, max_amount_inr=998, frequency="monthly", next_due_at=clock.now())
        db.add(m)
        db.commit()
        failed_debit = {"payment": {"entity": {
            "id": "pay_debit_fail1", "amount": 49900, "status": "failed", "order_id": "order_x", "recurring": True,
            "customer_id": "cust_eval_m1", "token_id": "token_eval_1", "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "insufficient_funds", "error_description": "insufficient funds",
            "notes": {"rr_kind": "mandate_debit", "rr_mandate": m.id, "rr_token": "token_eval_1", "rr_purpose": "cycle"}}}}
        _webhook(client, "payment.failed", failed_debit, "evt_m_1")
        run_ = db.query(MandateRun).filter_by(mandate_id=m.id).first()
        R.case("mandate:failed_debit_opens_card_run", run_ is not None and run_.subscription_id == "token_eval_1"
               and abs(run_.amount_inr - 499) < 0.01, f"run={run_ and (run_.subscription_id, run_.amount_inr)}", critical=True)
        pdn = jobs("mandate_run", run_.id, ScheduledKind.MANDATE_PDN) if run_ else []
        stub_retry = jobs("mandate_run", run_.id, ScheduledKind.MANDATE_RETRY) if run_ else []
        debit_at = datetime.fromisoformat(json.loads(pdn[0].payload_json)["debit_at"]) if pdn else None
        R.case("mandate:retry_is_a_real_debit_37h_out", len(pdn) == 1 and not stub_retry
               and debit_at >= clock.now() + CARD_TOKEN_DEBIT_LEAD - timedelta(minutes=1),
               f"pdn={len(pdn)} stub_retry={len(stub_retry)} debit_at={debit_at}", critical=True)

        h = scheduler._HANDLERS
        st, label = h[ScheduledKind.MANDATE_PDN.value](db, pdn[0], json.loads(pdn[0].payload_json))
        order_body = list(fake.orders.values())[-1] if fake.orders else {}
        debit_jobs = jobs("mandate_run", run_.id, ScheduledKind.MANDATE_DEBIT)
        R.case("mandate:pdn_creates_order_with_notification", label.startswith("pdn_created")
               and order_body.get("notification", {}).get("token_id") == "token_eval_1" and len(debit_jobs) == 1,
               f"label={label} order={order_body.get('notification')} debits={len(debit_jobs)}", critical=True)

        # promise moves the real debit
        today = (clock.now() + timedelta(hours=5, minutes=30)).date()
        p, code, _ = promises.create_promise(db, "mandate_run", run_.id, today + timedelta(days=4), "merchant_manual")
        moved = jobs("mandate_run", run_.id, ScheduledKind.MANDATE_PDN)
        moved_at = datetime.fromisoformat(json.loads(moved[0].payload_json)["debit_at"]) if moved else None
        R.case("mandate:promise_moves_the_debit", p is not None and len(moved) == 1 and not jobs("mandate_run", run_.id, ScheduledKind.MANDATE_DEBIT)
               and moved_at.date() >= today + timedelta(days=3), f"promise={code} pdn={len(moved)} debit_at={moved_at}", critical=True)

        # pay-now link settles the run and cancels the queued debit
        led4 = _ledger(db)
        rl = autopay.run_payment_link(db, run_)
        _webhook(client, "payment_link.paid", _link_paid_payload(rl, 499, "pay_run_link"), "evt_m_2")
        db.refresh(run_)
        led5 = _ledger(db)
        R.case("mandate:pay_now_link_settles_run", run_.risk_settled and run_.status == MandateStatus.RECOVERED.value
               and not jobs("mandate_run", run_.id, ScheduledKind.MANDATE_PDN)
               and abs(led5["total_revenue"] - led4["total_revenue"] - 499) < 0.01,
               f"settled={run_.risk_settled} status={run_.status} rev {led4['total_revenue']}->{led5['total_revenue']}", critical=True)
        n_pay = len(fake.payments)
        st2, label2 = h[ScheduledKind.MANDATE_DEBIT.value](db, debit_jobs[0], json.loads(debit_jobs[0].payload_json))
        R.case("mandate:no_debit_on_settled_run", st2 == ScheduledStatus.SKIPPED.value and len(fake.payments) == n_pay,
               f"{st2} {label2}", critical=True)

        # debit captured -> revenue once, open run recovered; token.cancelled stops debits
        m2 = Mandate(customer_name="Eval Autopay 2", contact="+919812300003", razorpay_customer_id="cust_eval_m2",
                     token_id="token_eval_2", status=MandateState.ACTIVE.value, amount_inr=799, max_amount_inr=1598,
                     frequency="monthly", next_due_at=clock.now())
        db.add(m2)
        db.commit()
        _webhook(client, "payment.failed", {"payment": {"entity": {**failed_debit["payment"]["entity"], "id": "pay_debit_fail2",
                 "amount": 79900, "customer_id": "cust_eval_m2", "token_id": "token_eval_2",
                 "notes": {"rr_kind": "mandate_debit", "rr_mandate": m2.id, "rr_token": "token_eval_2", "rr_purpose": "cycle"}}}}, "evt_m_3")
        run2 = db.query(MandateRun).filter_by(mandate_id=m2.id).first()
        led6 = _ledger(db)
        ok_pay = {"payment": {"entity": {"id": "pay_debit_ok2", "amount": 79900, "status": "captured", "recurring": True,
                                         "token_id": "token_eval_2", "notes": {"rr_kind": "mandate_debit", "rr_mandate": m2.id,
                                                                              "rr_token": "token_eval_2", "rr_purpose": "retry", "rr_run": run2.id}}}}
        _webhook(client, "payment.captured", ok_pay, "evt_m_4")
        _webhook(client, "payment.captured", ok_pay, "evt_m_4")   # redelivery
        db.refresh(run2)
        led7 = _ledger(db)
        R.case("mandate:debit_captured_recovers_once", run2.status == MandateStatus.RECOVERED.value
               and abs(led7["total_revenue"] - led6["total_revenue"] - 799) < 0.01
               and abs(led7["total_recovered"] - led6["total_recovered"] - 799) < 0.01,
               f"status={run2.status} rev {led6['total_revenue']}->{led7['total_revenue']}", critical=True)

        m3 = Mandate(customer_name="Eval Autopay 3", razorpay_customer_id="cust_eval_m3", token_id="token_eval_3",
                     status=MandateState.ACTIVE.value, amount_inr=299, max_amount_inr=598, frequency="monthly",
                     next_due_at=clock.now() + timedelta(days=5))
        db.add(m3)
        db.commit()
        autopay.schedule_debit(db, m3, clock.now() + timedelta(days=5))
        _webhook(client, "token.cancelled", {"token": {"entity": {"id": "token_eval_3", "customer_id": "cust_eval_m3"}}}, "evt_m_5")
        db.refresh(m3)
        R.case("mandate:token_cancelled_stops_debits", m3.status == MandateState.CANCELLED.value
               and not jobs("mandate", m3.id, ScheduledKind.MANDATE_PDN), f"status={m3.status}", critical=True)

        # ── Safety ────────────────────────────────────────────────────────────
        settings.razorpay_key_id = "rzp_live_eval"
        try:
            rz.fetch_payment("pay_x")
            refused = False
        except rz.RazorpayError as e:
            refused = e.code == "live_key_outside_production"
        R.case("safety:live_key_refused_outside_production", refused, "a live key was used outside production", critical=True)
        settings.razorpay_key_id = "rzp_test_eval"
        with payment_links.force_stub():
            n_before = len(fake.links)
            cp6, inv6 = fx.invoice_customer(db, amount=700)
            notify.invoice_message(db, inv6, "invoice_overdue")
        R.case("safety:force_stub_spends_no_slot", len(fake.links) == n_before, f"{n_before} -> {len(fake.links)}")

        total = len(R.result.cases)
        R.rate("pass_rate", total - len([c for c in R.result.cases if not c.ok]), total)
        R.require("pass_rate", ">=", 1.0, "money paths must all hold")
        return R.result
    finally:
        rz.set_transport(None)
        (settings.razorpay_links, settings.razorpay_key_id, settings.razorpay_key_secret,
         settings.razorpay_webhook_secret) = saved
        db.close()

"""Logins and roles, the customer autopay page, merchant-registered invoices, and the Simulation Console: nobody reaches another role's or person's data, permissions never come from the merchant, and the run-to-the-end buttons spend no real Razorpay link or debit."""
import json
from datetime import timedelta

import httpx

from evals import fixtures as fx
from evals.harness import Recorder
from evals.suites.razorpay_money import FakeRazorpay


class Fake(FakeRazorpay):
    """FakeRazorpay plus the customer / registration / link-fetch calls the autopay page makes."""

    def __init__(self):
        super().__init__(link_cap=1000)
        self.paid_links: dict[str, dict] = {}     # link id -> payment to list on fetch
        self.customers = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/v1/customers":
            self.calls.append((method, path))
            self.customers += 1
            return httpx.Response(200, json={"id": self._id("cust")})
        if method == "POST" and path == "/v1/subscription_registration/auth_links":
            self.calls.append((method, path))
            iid = self._id("inv")
            return httpx.Response(200, json={"id": iid, "short_url": f"https://rzp.io/i/{iid[-6:]}", "status": "issued"})
        if method == "DELETE" and path.startswith("/v1/customers/"):
            self.calls.append((method, path))
            return httpx.Response(200, json={"deleted": True})
        if method == "GET" and path.startswith("/v1/payment_links/"):
            self.calls.append((method, path))
            lid = path.rsplit("/", 1)[1]
            link = dict(self.links.get(lid) or {"id": lid})
            if lid in self.paid_links:
                link["status"] = "paid"
                link["payments"] = [self.paid_links[lid]]
            return httpx.Response(200, json=link)
        return super().handler(request)


def run(ctx):
    from fastapi.testclient import TestClient
    from app import accounts, autopay, clock, contacts, notify, payment_links, promises, receivables, scheduler, voice
    from app import razorpay_client as rz, runtime_flags
    from app.auth import hash_password
    from app.config import settings
    from app.database import SessionLocal
    from app.main import app
    from app.models import (
        AppUser, AuditLog, BusinessAccount, ContactPoint, Customer, Invoice, Mandate, MandateRun, MandateState, MandateStatus,
        PaymentPromise, ScheduledAction, ScheduledKind, ScheduledStatus,
    )
    from app.revenue import as_dict, get_or_create_state
    from app.rules_engine import CARD_TOKEN_DEBIT_LEAD

    fx.patch_world()
    R = Recorder("accounts_console", "Roles gate every page and endpoint; a person only ever sees their own mandates and invoices; the "
                                     "merchant cannot grant consent; the run-to-the-end buttons spend no real link and no real debit.")
    db = SessionLocal()
    fake = Fake()
    saved = (settings.razorpay_links, settings.razorpay_key_id, settings.razorpay_key_secret, settings.merchant_api_key)
    settings.razorpay_links, settings.razorpay_key_id, settings.razorpay_key_secret = "real", "rzp_test_eval", "eval_secret"
    settings.merchant_api_key = ""
    rz.set_transport(httpx.MockTransport(fake.handler))
    runtime_flags.set_sim_real_links(None)
    runtime_flags.set_sim_debit_outcome(None)
    client = TestClient(app)
    ledger = lambda: (db.expire_all(), as_dict(get_or_create_state(db)))[1]
    H = lambda tok: {"Authorization": f"Bearer {tok}"}

    def login(email, pw):
        r = client.post("/auth/login", json={"email": email, "password": pw})
        return r.status_code, (r.json() if r.status_code == 200 else {})

    def shopper(name, email, phone):
        c = Customer(email=email, password_hash=hash_password("shopper-pass-1"), name=name)
        db.add(c)
        db.commit()
        db.refresh(c)
        contacts.upsert_contact(db, customer_id=c.id, phone=phone, language="hinglish", verified_now=True)
        return c

    try:
        # ── Logins and roles ────────────────────────────────────────────────────
        R.case("open:no_merchant_means_open_dashboard", client.get("/merchant/business-customers").status_code == 200,
               "a fresh clone must still demo", critical=True)
        try:
            accounts.create_merchant(db, "merchant@eval.invalid", "short", "x")
            short_ok = True
        except ValueError:
            short_ok = False
        R.case("merchant:short_password_refused", not short_ok, "an 5-character password was accepted")
        merchant = accounts.create_merchant(db, "merchant@eval.invalid", "merchant-pass-1", "Eval Store")
        try:
            accounts.create_merchant(db, "second@eval.invalid", "merchant-pass-2", "Two")
            second = True
        except ValueError:
            second = False
        R.case("merchant:only_one", not second, "a second merchant account was created", critical=True)

        c1 = shopper("Asha Customer", "asha@eval.invalid", "+919811100001")
        c2 = shopper("Bela Customer", "bela@eval.invalid", "+919811100002")
        s, ms = login("merchant@eval.invalid", "merchant-pass-1")
        s2, cs1 = login("asha@eval.invalid", "shopper-pass-1")
        s3, cs2 = login("bela@eval.invalid", "shopper-pass-1")
        R.case("login:roles_returned", s == 200 and ms.get("role") == "merchant" and s2 == 200 and cs1.get("role") == "customer",
               f"{s}/{ms.get('role')} {s2}/{cs1.get('role')}", critical=True)
        R.case("login:wrong_password_refused", login("asha@eval.invalid", "nope")[0] == 401
               and login("merchant@eval.invalid", "nope")[0] == 401 and login("ghost@eval.invalid", "x")[0] == 401, "bad login accepted", critical=True)
        mt, t1, t2 = ms["token"], cs1["token"], cs2["token"]

        gate = lambda path, tok=None: client.get(path, headers=H(tok) if tok else {}).status_code
        R.case("gate:merchant_routes_need_the_merchant", gate("/merchant/business-customers") == 401
               and gate("/merchant/business-customers", t1) == 401 and gate("/merchant/business-customers", mt) == 200,
               f"none={gate('/merchant/business-customers')} customer={gate('/merchant/business-customers', t1)}", critical=True)
        R.case("gate:demo_console_needs_the_merchant", gate("/demo/state") == 401 and gate("/demo/state", t1) == 401
               and gate("/demo/state", mt) == 200, "the Demo Console is reachable without the merchant", critical=True)
        R.case("gate:shopper_token_not_a_merchant_in_storefront_only", gate("/auth/me", t1) == 200 and gate("/auth/me", mt) == 401,
               "a merchant token worked as a shopper", critical=True)
        R.case("gate:whoami_any_role", client.get("/auth/whoami", headers=H(mt)).json().get("role") == "merchant"
               and client.get("/auth/whoami", headers=H(t1)).json().get("role") == "customer", "whoami wrong")

        # ── Merchant registers businesses and invoices; businesses see only their own ──
        r = client.post("/merchant/business-customers", headers=H(mt), json={
            "company_name": "Alpha Traders", "contact_name": "Alok", "email": "alpha@eval.invalid", "phone": "+919822200001"})
        r2 = client.post("/merchant/business-customers", headers=H(mt), json={
            "company_name": "Beta Traders", "contact_name": "Bhavna", "email": "beta@eval.invalid", "phone": "+919822200002"})
        a, b = r.json(), r2.json()
        R.case("business:created_with_login", r.status_code == 200 and a.get("temporary_password") and a.get("account_id"),
               f"{r.status_code} {r.text[:120]}", critical=True)
        n_acc = db.query(BusinessAccount).count()
        bad = client.post("/merchant/business-customers", headers=H(mt), json={"company_name": "Bad Phone Co", "email": "badphone@eval.invalid", "phone": "123"})
        R.case("business:bad_number_leaves_nothing_behind", bad.status_code == 422 and db.query(BusinessAccount).count() == n_acc,
               f"{bad.status_code} accounts {n_acc}->{db.query(BusinessAccount).count()}", critical=True)
        R.case("business:duplicate_email_refused", client.post("/merchant/business-customers", headers=H(mt), json={
            "company_name": "Alpha Two", "email": "alpha@eval.invalid"}).status_code == 409
               and client.post("/merchant/business-customers", headers=H(mt), json={
                   "company_name": "Shopper Clash", "email": "asha@eval.invalid"}).status_code == 409, "an email was reused")
        stored = db.query(AppUser).filter_by(email="alpha@eval.invalid").first()
        R.case("business:password_not_stored_in_clear", stored is not None and a["temporary_password"] not in stored.password_hash,
               "the temporary password is stored in the clear", critical=True)

        due = (clock.now() + timedelta(days=5)).isoformat()
        i1 = client.post("/merchant/invoices", headers=H(mt), json={"account_id": a["account_id"], "amount_inr": 5000, "due_at": due})
        i2 = client.post("/merchant/invoices", headers=H(mt), json={"account_id": a["account_id"], "amount_inr": 700, "due_at": due})
        i3 = client.post("/merchant/invoices", headers=H(mt), json={"account_id": b["account_id"], "amount_inr": 900, "due_at": due})
        nos = {i1.json().get("invoice_no"), i2.json().get("invoice_no"), i3.json().get("invoice_no")}
        R.case("invoice:auto_numbers_unique", i1.status_code == 200 and len(nos) == 3 and None not in nos, f"{nos}", critical=True)
        iz = client.post("/merchant/invoices", headers=H(mt), json={"account_id": a["account_id"], "amount_inr": 100,
                                                                    "due_at": (clock.now() + timedelta(days=9)).isoformat() + "Z"})
        R.case("invoice:browser_style_utc_due_date_accepted", iz.status_code == 200, f"{iz.status_code} {iz.text[:120]}", critical=True)
        R.case("invoice:bad_amount_refused", client.post("/merchant/invoices", headers=H(mt), json={
            "account_id": a["account_id"], "amount_inr": 0, "due_at": due}).status_code == 422, "a zero invoice was accepted")

        # ── Merchant edits what it typed: email, number, name - never consent ──
        before_login = login("alpha@eval.invalid", a["temporary_password"])[0]
        up = client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(mt), json={
            "contact_name": "Alok Kumar", "email": "alpha.new@eval.invalid", "phone": "+919822200011", "preferred_language": "hi"})
        R.case("edit:merchant_updates_details", up.status_code == 200 and {"email", "phone", "contact_name"} <= set(up.json()["updated"]), f"{up.status_code} {up.text[:120]}", critical=True)
        R.case("edit:login_follows_the_new_email", login("alpha@eval.invalid", a["temporary_password"])[0] == 401
               and login("alpha.new@eval.invalid", a["temporary_password"])[0] == 200 and before_login == 200, "login email did not move", critical=True)
        db.expire_all()
        cp_e = contacts.for_business_account(db, a["account_id"])
        R.case("edit:new_number_is_unverified_without_consent", cp_e.phone == "+919822200011" and cp_e.phone_verified_at is None
               and not cp_e.voice_consent and cp_e.preferred_language == "hi", f"{cp_e.phone} verified={cp_e.phone_verified_at}", critical=True)
        R.case("edit:duplicate_email_refused", client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(mt), json={"email": "asha@eval.invalid"}).status_code == 409
               and client.put(f"/merchant/business-customers/{b['account_id']}", headers=H(mt), json={"email": "alpha.new@eval.invalid"}).status_code == 409, "an email was reused")
        R.case("edit:consent_not_editable_by_merchant", client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(mt), json={"voice_consent": True}).status_code == 200
               and not contacts.for_business_account(db, a["account_id"]).voice_consent, "the merchant set consent")
        R.case("edit:bad_values_refused", client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(mt), json={"phone": "123"}).status_code == 422
               and client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(mt), json={"email": "nope"}).status_code == 422
               and client.put("/merchant/business-customers/not-a-uuid", headers=H(mt), json={}).status_code == 404, "")
        rp = client.post(f"/merchant/business-customers/{a['account_id']}/reset-password", headers=H(mt))
        R.case("edit:reset_password_works_once_old_fails", rp.status_code == 200 and login("alpha.new@eval.invalid", rp.json()["temporary_password"])[0] == 200
               and login("alpha.new@eval.invalid", a["temporary_password"])[0] == 401, f"{rp.status_code}")
        a["temporary_password"] = rp.json()["temporary_password"]

        _, ba = login("alpha.new@eval.invalid", a["temporary_password"])
        _, bb = login("beta@eval.invalid", b["temporary_password"])
        R.case("login:business_role", ba.get("role") == "business", f"{ba.get('role')}", critical=True)
        ta, tb = ba["token"], bb["token"]
        R.case("edit:only_the_merchant", client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(ta), json={"company_name": "x"}).status_code == 401
               and client.put(f"/merchant/business-customers/{a['account_id']}", headers=H(t1), json={"company_name": "x"}).status_code == 401, "a non-merchant edited", critical=True)
        mine_a = client.get("/biz/invoices", headers=H(ta)).json()
        mine_b = client.get("/biz/invoices", headers=H(tb)).json()
        R.case("business:sees_only_own_invoices", {i["invoice_no"] for i in mine_a["invoices"]} == {i1.json()["invoice_no"], i2.json()["invoice_no"], iz.json()["invoice_no"]}
               and {i["invoice_no"] for i in mine_b["invoices"]} == {i3.json()["invoice_no"]}, "invoice lists leaked across businesses", critical=True)
        R.case("gate:roles_do_not_cross", gate("/biz/invoices", t1) in (401, 403) and gate("/biz/invoices", mt) in (401, 403)
               and gate("/biz/invoices") == 401 and gate("/me/autopay", ta) == 401 and gate("/me/autopay", mt) == 401
               and gate("/merchant/business-customers", ta) == 401 and gate("/demo/state", ta) == 401,
               "a role reached another role's page", critical=True)
        other_inv = i3.json()["id"]
        R.case("business:cannot_pay_link_of_another", client.post(f"/biz/invoices/{other_inv}/pay-link", headers=H(ta)).status_code == 404,
               "a business opened another's invoice", critical=True)
        links_before = len(fake.links)
        own = client.post(f"/biz/invoices/{i1.json()['id']}/pay-link", headers=H(ta))
        again = client.post(f"/biz/invoices/{i1.json()['id']}/pay-link", headers=H(ta))
        R.case("business:pay_now_makes_one_link_and_reuses", own.status_code == 200 and not own.json()["is_stub"]
               and len(fake.links) == links_before + 1 and own.json()["url"] == again.json()["url"],
               f"{own.status_code} {own.text[:100]} links {links_before}->{len(fake.links)}")
        runtime_flags.set_sim_real_links(False)          # the console's switch is OFF ...
        sw = client.post(f"/biz/invoices/{i1.json()['id']}/pay-link", headers=H(ta))
        with payment_links.customer_action():
            customer_mode = payment_links.mode()
        R.case("business:pay_now_ignores_console_switch", sw.status_code == 200 and not sw.json()["is_stub"]
               and customer_mode == "real" and payment_links.mode() == "stub",
               f"{sw.status_code} {sw.text[:100]} customer={customer_mode}", critical=True)
        runtime_flags.set_sim_real_links(None)

        # ── Permissions: only the person grants them, only after verifying ───────
        def put_biz(tok, body):
            return client.put("/biz/contact", headers=H(tok), json=body)
        no_verify = put_biz(ta, {"voice_consent": True})
        R.case("permissions:no_consent_before_verification", no_verify.status_code == 409, f"{no_verify.status_code}", critical=True)
        cp_a = contacts.for_business_account(db, a["account_id"])
        cp_a.phone_verified_at = clock.now()
        db.commit()
        ok = put_biz(ta, {"voice_consent": True, "preferred_language": "en", "memory_consent": True})
        db.expire_all()
        cp_a = contacts.for_business_account(db, a["account_id"])
        R.case("permissions:customer_consent_recorded", ok.status_code == 200 and cp_a.voice_consent and cp_a.consent_source == "customer_ticked"
               and cp_a.preferred_language == "en" and contacts.has_full_memory_consent(cp_a), f"{ok.status_code} {ok.text[:100]}", critical=True)
        notes_on = " ".join(n["text"] for n in ok.json()["notes"])
        off = put_biz(ta, {"voice_consent": False, "memory_consent": False})
        notes_off = " ".join(n["text"] for n in off.json()["notes"])
        R.case("permissions:instructions_follow_choices", "You allowed calls" in notes_on and "remember our calls" in notes_on
               and "only text you" in notes_off and "do not keep notes" in notes_off,
               f"on={notes_on[:80]!r} off={notes_off[:80]!r}")
        db.expire_all()
        R.case("permissions:merchant_cannot_tick_for_them", contacts.for_business_account(db, a["account_id"]).voice_consent is False
               and client.put("/biz/contact", headers=H(mt), json={"voice_consent": True}).status_code in (401, 403),
               "the merchant granted a business's consent", critical=True)
        R.case("permissions:bad_language_refused", put_biz(ta, {"preferred_language": "klingon"}).status_code == 422, "")

        # ── Customer autopay page ────────────────────────────────────────────────
        svc = client.get("/me/autopay/services").json()
        R.case("autopay:three_services_and_custom", len(svc["services"]) == 3 and svc["custom"]["min_inr"] < svc["custom"]["max_inr"], f"{svc}")
        R.case("autopay:menu_price_wins", client.post("/me/autopay", headers=H(t1), json={"service_key": "plus", "amount_inr": 1}).json().get("amount_inr") == 499,
               "a client-sent amount overrode the menu price", critical=True)
        n_cust = fake.customers
        first = client.get("/me/autopay", headers=H(t1)).json()
        dup = client.post("/me/autopay", headers=H(t1), json={"service_key": "plus"})
        R.case("autopay:unfinished_setup_is_reused", dup.status_code == 200 and dup.json()["id"] == first[0]["id"]
               and fake.customers == n_cust and dup.json()["registration_url"], f"customers {n_cust}->{fake.customers}")
        for body, label in (({"service_key": "custom", "amount_inr": 5, "frequency": "monthly"}, "below_min"),
                            ({"service_key": "custom", "amount_inr": 999999, "frequency": "monthly"}, "above_max"),
                            ({"service_key": "custom", "amount_inr": 100, "frequency": "daily"}, "bad_frequency"),
                            ({"service_key": "nope"}, "unknown")):
            R.case(f"autopay:custom_{label}_refused", client.post("/me/autopay", headers=H(t1), json=body).status_code == 422, label)
        okc = client.post("/me/autopay", headers=H(t1), json={"service_key": "custom", "amount_inr": 250, "frequency": "weekly"})
        R.case("autopay:custom_amount_accepted", okc.status_code == 200 and okc.json()["amount_inr"] == 250 and okc.json()["frequency"] == "weekly", okc.text[:120])
        mine = db.query(Mandate).filter(Mandate.customer_id == c1.id).all()
        cps = db.query(ContactPoint).filter(ContactPoint.phone == "+919811100001").count()
        R.case("autopay:contact_linked_to_the_customer_row", mine and mine[0].razorpay_customer_id
               and contacts.for_customer(db, c1.id).razorpay_customer_id == mine[0].razorpay_customer_id and cps == 1,
               f"contact points for the number: {cps}", critical=True)
        mid = first[0]["id"]
        R.case("autopay:other_customer_cannot_touch", client.post(f"/me/autopay/{mid}/sync", headers=H(t2)).status_code == 404
               and client.post(f"/me/autopay/{mid}/cancel", headers=H(t2)).status_code == 404
               and client.get("/me/autopay", headers=H(t2)).json() == [], "customer B reached customer A's mandate", critical=True)
        R.case("autopay:merchant_token_is_not_a_shopper", client.post("/me/autopay", headers=H(mt), json={"service_key": "plus"}).status_code == 401, "")

        m = db.query(Mandate).filter(Mandate.id == mid).first()
        autopay._activate(db, m, "token_eval_acct", "eval")
        view = client.get("/me/autopay", headers=H(t1)).json()
        act = next(v for v in view if v["id"] == mid)
        R.case("autopay:overview_shows_next_debit", act["status"] == "active" and act["next_debit_at"] and act["next_debit_amount_inr"] == 499
               and act["notice_at"] and act["notes"], f"{act['status']} {act['next_debit_at']}", critical=True)
        gap = (autopay_dt(act["next_debit_at"]) - autopay_dt(act["notice_at"])).total_seconds() / 3600
        R.case("autopay:notice_is_37h_before_debit", abs(gap - CARD_TOKEN_DEBIT_LEAD.total_seconds() / 3600) < 0.5, f"gap={gap:.2f}h")
        pr = client.put("/me/autopay/permissions", headers=H(t1), json={"voice_consent": True, "preferred_language": "hi"})
        txt = " ".join(n["text"] for n in pr.json()["notes"])
        R.case("autopay:permissions_step_changes_instructions", pr.status_code == 200 and "You allowed calls" in txt and "Hindi" in txt
               and contacts.can_call(contacts.for_customer(db, c1.id))[0], txt[:120], critical=True)
        gone = client.post(f"/me/autopay/{mid}/cancel", headers=H(t1))
        db.expire_all()
        pend = db.query(ScheduledAction).filter(ScheduledAction.subject_id == mid, ScheduledAction.status == ScheduledStatus.SCHEDULED.value,
                                                ScheduledAction.kind.in_([ScheduledKind.MANDATE_PDN.value, ScheduledKind.MANDATE_DEBIT.value])).count()
        R.case("autopay:cancel_stops_every_debit", gone.json()["status"] == "cancelled" and pend == 0, f"{gone.json()['status']} pending={pend}", critical=True)

        # ── Simulation Console ──────────────────────────────────────────────────
        modes = client.get("/demo/modes", headers=H(mt)).json()
        R.case("console:modes_report", modes["debit"] == "real" and "window_days" in modes and "links" in modes, f"{modes}")
        client.post("/demo/links-mode", headers=H(mt), json={"real": False})
        R.case("console:links_switch_forces_stand_ins", payment_links.mode() == "stub" and runtime_flags.get_sim_real_links() is False,
               "the switch did not take", critical=True)
        n0 = len(fake.links)
        cpx, invx = fx.invoice_customer(db, amount=3100)
        notify.invoice_message(db, invx, "invoice_overdue")
        R.case("console:switch_off_spends_no_link", len(fake.links) == n0, f"{n0}->{len(fake.links)}", critical=True)
        client.post("/demo/links-mode", headers=H(mt), json={"real": True})
        notify.invoice_message(db, invx, "invoice_overdue")
        R.case("console:switch_on_spends_a_real_link", len(fake.links) == n0 + 1 and payment_links.mode() == "real", f"{n0}->{len(fake.links)}")
        client.post("/demo/links-mode", headers=H(mt), json={"real": False})

        q = client.get("/demo/queue", params={"subject_type": "invoice", "subject_id": invx.id}, headers=H(mt)).json()
        runs = [i["runs_at"] for i in q["items"]]
        R.case("queue:soonest_first_with_plain_text", q["items"] and runs == sorted(runs) and all(i["title"] for i in q["items"]), f"{runs[:3]}")
        R.case("queue:customer_steps_wait_for_the_window", all(scheduler.in_contact_window(autopay_dt(i["runs_at"])) for i in q["items"] if i["customer_facing"]),
               "a customer-facing step is shown outside 08:00-19:00 IST", critical=True)

        # a real autopay step far ahead warns; a played result does not
        mm = Mandate(customer_name="Queue Eval", contact="+919833300001", email="q@eval.invalid", razorpay_customer_id="cust_q1",
                     token_id="token_q1", status=MandateState.ACTIVE.value, amount_inr=300, max_amount_inr=600, frequency="monthly", next_due_at=clock.now())
        db.add(mm)
        db.commit()
        row = autopay.schedule_debit(db, mm, clock.now() + timedelta(days=10))
        qq = client.get("/demo/queue", params={"subject_type": "invoice", "subject_id": invx.id}, headers=H(mt))
        # queue is per debt: read this mandate's jobs through the same helper the endpoint uses
        from app import demo
        far = [r for r in demo._queued_rows(db, mm.id)]
        R.case("queue:autopay_jobs_listed_under_the_mandate", any(r.kind == ScheduledKind.MANDATE_PDN.value for r in far), f"{[r.kind for r in far]}")
        db.query(ScheduledAction).filter(ScheduledAction.subject_id == mm.id).delete()
        db.commit()

        # ── Finish-unpaid: invoice ───────────────────────────────────────────────
        voice.set_interactive_mode(True)
        voice.set_simulated_scenario("pay_now")
        runtime_flags.set_sim_real_links(True)       # real links ALLOWED: only the button itself must keep them unspent
        cpf, invf = fx.invoice_customer(db, amount=4200)
        links_n, orders_n, pays_n = len(fake.links), len(fake.orders), len(fake.payments)
        res = client.post("/demo/finish-unpaid", headers=H(mt), json={"subject_type": "invoice", "subject_id": invf.id})
        body = res.json()
        db.expire_all()
        invf = db.query(Invoice).filter(Invoice.id == invf.id).first()
        left = db.query(ScheduledAction).filter(ScheduledAction.subject_id == invf.id, ScheduledAction.status == ScheduledStatus.SCHEDULED.value).count()
        R.case("finish:invoice_walks_to_the_end", res.status_code == 200 and body["steps"] and left == 0 and not invf.settled
               and invf.escalated_at is not None, f"{res.status_code} steps={len(body.get('steps', []))} left={left} escalated={invf.escalated_at}", critical=True)
        R.case("finish:invoice_spends_no_real_link", len(fake.links) == links_n and body["real_links_spent"] == 0, f"{links_n}->{len(fake.links)}", critical=True)
        R.case("finish:console_settings_restored", voice.interactive_mode() is True and voice._sim["scenario"] == "pay_now"
               and runtime_flags.get_sim_real_links() is True and payment_links.mode() == "real",
               f"interactive={voice.interactive_mode()} scenario={voice._sim['scenario']}", critical=True)
        voice.set_interactive_mode(False)
        runtime_flags.set_sim_real_links(False)
        R.case("finish:unknown_subject_404", client.post("/demo/finish-unpaid", headers=H(mt), json={"subject_type": "invoice", "subject_id": "nope"}).status_code in (404, 422), "")

        # ── Finish-unpaid: card-mandate run never asks Razorpay to debit ─────────
        mr = Mandate(customer_name="Finish Eval", contact="+919844400001", email="f@eval.invalid", razorpay_customer_id="cust_f1",
                     token_id="token_f1", status=MandateState.ACTIVE.value, amount_inr=499, max_amount_inr=998, frequency="monthly", next_due_at=clock.now())
        db.add(mr)
        db.commit()
        contacts.upsert_contact(db, razorpay_customer_id="cust_f1", phone="+919844400001")
        failed = {"payment": {"entity": {"id": "pay_f_fail1", "amount": 49900, "status": "failed", "order_id": "order_f", "recurring": True,
                                         "customer_id": "cust_f1", "token_id": "token_f1", "error_code": "BAD_REQUEST_ERROR",
                                         "error_reason": "insufficient_funds", "error_description": "insufficient funds",
                                         "notes": {"rr_kind": "mandate_debit", "rr_mandate": mr.id, "rr_token": "token_f1", "rr_purpose": "cycle"}}}}
        import hashlib, hmac as _h
        body_ = json.dumps({"event": "payment.failed", "payload": failed}).encode()
        sig = _h.new((settings.razorpay_webhook_secret or "eval_webhook_secret").encode(), body_, hashlib.sha256).hexdigest()
        settings.razorpay_webhook_secret = settings.razorpay_webhook_secret or "eval_webhook_secret"
        client.post("/webhooks/razorpay", content=body_, headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "evt_f_1", "Content-Type": "application/json"})
        db.expire_all()
        run = db.query(MandateRun).filter_by(mandate_id=mr.id).first()
        R.case("finish:card_run_opened", run is not None and not run.risk_settled, f"{run}", critical=True)
        lost_before = ledger()["total_lost"]
        orders_n, pays_n = len(fake.orders), len(fake.payments)
        res = client.post("/demo/finish-unpaid", headers=H(mt), json={"subject_type": "mandate_run", "subject_id": run.id})
        db.expire_all()
        run = db.query(MandateRun).filter_by(id=run.id).first()
        R.case("finish:card_run_never_asks_razorpay_to_debit", len(fake.orders) == orders_n and len(fake.payments) == pays_n
               and res.json().get("real_debits_made") == 0, f"orders {orders_n}->{len(fake.orders)} payments {pays_n}->{len(fake.payments)}", critical=True)
        R.case("finish:card_run_ends_lost_exactly_once", run.risk_settled and run.status in (MandateStatus.CANCELLED.value, MandateStatus.HALTED.value, MandateStatus.EXHAUSTED.value)
               and ledger()["total_lost"] - lost_before == run.amount_inr, f"status={run.status} lost {lost_before}->{ledger()['total_lost']}", critical=True)
        R.case("finish:simulated_debits_are_labelled", db.query(AuditLog).filter(AuditLog.action_type == "mandate_debit_simulated").count() >= 1,
               "a played debit left no audit entry")

        # ── Promise broken ───────────────────────────────────────────────────────
        cpp, invp = fx.invoice_customer(db, amount=2500)
        R.case("promise_broken:needs_a_promise", client.post("/demo/promise-broken", headers=H(mt), json={"subject_type": "invoice", "subject_id": invp.id}).status_code == 409, "")
        today = (clock.now() + timedelta(hours=5, minutes=30)).date()
        p, code, _ = promises.create_promise(db, "invoice", invp.id, today + timedelta(days=3), "merchant_manual")
        res = client.post("/demo/promise-broken", headers=H(mt), json={"subject_type": "invoice", "subject_id": invp.id})
        db.expire_all()
        p = db.query(PaymentPromise).filter_by(id=p.id).first()
        R.case("promise_broken:resolves_as_broken", res.status_code == 200 and p.status == "broken" and res.json()["real_links_spent"] == 0,
               f"{res.status_code} promise={p.status}", critical=True)

        # ── Played debit result and "check payment now" ─────────────────────────
        client.post("/demo/debit-mode", headers=H(mt), json={"outcome": "success"})
        ms_ = Mandate(customer_name="Played Eval", contact="+919855500001", email="p@eval.invalid", razorpay_customer_id="cust_p1",
                      token_id="token_p1", status=MandateState.ACTIVE.value, amount_inr=499, max_amount_inr=998, frequency="monthly", next_due_at=clock.now())
        db.add(ms_)
        db.commit()
        due0 = ms_.next_due_at
        autopay.schedule_debit(db, ms_, clock.now() + CARD_TOKEN_DEBIT_LEAD)
        rev0, orders_n, pays_n = ledger()["total_revenue"], len(fake.orders), len(fake.payments)
        t0 = clock.now()
        scheduler.run_due_once(db, now=t0 + timedelta(minutes=1))
        scheduler.run_due_once(db, now=t0 + CARD_TOKEN_DEBIT_LEAD + timedelta(hours=1))
        db.expire_all()
        ms_ = db.query(Mandate).filter_by(id=ms_.id).first()
        R.case("played_debit:success_books_once_and_asks_razorpay_nothing", abs(ledger()["total_revenue"] - rev0 - 499) < 0.01
               and len(fake.orders) == orders_n and len(fake.payments) == pays_n, f"revenue {rev0}->{ledger()['total_revenue']}", critical=True)
        R.case("played_debit:next_cycle_queued", ms_.next_due_at and ms_.next_due_at > due0 and db.query(ScheduledAction).filter(
            ScheduledAction.subject_id == ms_.id, ScheduledAction.status == ScheduledStatus.SCHEDULED.value).count() >= 1, f"{ms_.next_due_at}")
        client.post("/demo/debit-mode", headers=H(mt), json={"outcome": "real"})
        R.case("played_debit:real_restored", runtime_flags.get_sim_debit_outcome() is None, "")

        # ── An authorised autopay is a console item: pick it, press Next event, follow a failed debit ──
        mc = Mandate(customer_name="Console Autopay", contact="+919866600001", email="ca@eval.invalid", razorpay_customer_id="cust_ca1",
                     token_id="token_ca1", status=MandateState.ACTIVE.value, amount_inr=499, max_amount_inr=998, frequency="monthly",
                     service_name="Backstop Plus", next_due_at=clock.now())
        db.add(mc)
        db.commit()
        autopay.schedule_debit(db, mc, clock.now() + CARD_TOKEN_DEBIT_LEAD)
        subs = client.get("/demo/subjects", headers=H(mt)).json()
        mine = [x for x in subs if x["subject_id"] == mc.id]
        R.case("autopay_item:listed_for_the_merchant", len(mine) == 1 and mine[0]["subject_type"] == "mandate" and "Backstop Plus" in mine[0]["title"]
               and mine[0]["next_action_at"] is not None, f"{mine}", critical=True)
        qm = client.get("/demo/queue", params={"subject_type": "mandate", "subject_id": mc.id}, headers=H(mt))
        R.case("autopay_item:queue_shows_the_debit_steps", qm.status_code == 200 and len(qm.json()["items"]) >= 1 and qm.json()["follow_up"] is None,
               f"{qm.status_code} {qm.text[:160]}", critical=True)
        R.case("autopay_item:timeline_money_memory_answer", all(client.get(u, params=pr, headers=H(mt)).status_code == 200 for u, pr in (
            ("/demo/timeline", {"subject_type": "mandate", "subject_id": mc.id}),
            ("/demo/money", {"subject_type": "mandate", "subject_id": mc.id}),
            ("/demo/memory", {"subject_type": "mandate", "subject_id": mc.id}))), "an endpoint refused the autopay item")
        client.post("/demo/debit-mode", headers=H(mt), json={"outcome": "insufficient_funds"})
        for _ in range(3):
            client.post("/demo/clock/next", params={"subject_id": mc.id}, headers=H(mt))
        qm2 = client.get("/demo/queue", params={"subject_type": "mandate", "subject_id": mc.id}, headers=H(mt)).json()
        R.case("autopay_item:failed_debit_points_to_its_run", qm2["follow_up"] is not None and qm2["follow_up"]["subject_type"] == "mandate_run",
               f"{qm2['follow_up']}", critical=True)
        client.post("/demo/debit-mode", headers=H(mt), json={"outcome": "real"})
        # an autopay with an empty queue: Next event queues the next debit itself instead of doing nothing
        me = Mandate(customer_name="Empty Queue", contact="+919866600002", email="eq@eval.invalid", razorpay_customer_id="cust_eq1",
                     token_id="token_eq1", status=MandateState.ACTIVE.value, amount_inr=299, max_amount_inr=598, frequency="monthly", next_due_at=clock.now())
        db.add(me)
        db.commit()
        before_q = client.get("/demo/queue", params={"subject_type": "mandate", "subject_id": me.id}, headers=H(mt)).json()["items"]
        nx = client.post("/demo/clock/next", params={"subject_id": me.id}, headers=H(mt))
        db.expire_all()
        R.case("autopay_item:next_event_queues_the_next_debit_when_empty", len(before_q) == 0 and nx.status_code == 200
               and db.query(ScheduledAction).filter(ScheduledAction.subject_id == me.id).count() >= 1, f"{nx.status_code} {nx.text[:140]}", critical=True)
        # Razorpay refusing a debit because its real-time window is not open yet must re-queue it, whichever wording it used
        R.case("autopay_item:timing_refusal_wordings_are_recognised", all(autopay._is_timing_refusal(rz.RazorpayError(400, c, d)) for c, d in (
            ("PROMISED_DEBIT_DATE", "x"), ("BAD_REQUEST_ERROR", "Debit can be attempted 36 hours after sending the pre-debit notification")))
               and not autopay._is_timing_refusal(rz.RazorpayError(400, "BAD_REQUEST_ERROR", "token is cancelled")), "a timing refusal would drop the debit", critical=True)

        cpl, invl = fx.invoice_customer(db, amount=1800)
        runtime_flags.set_sim_real_links(True)
        lk = receivables.invoice_link(db, invl)
        runtime_flags.set_sim_real_links(False)
        fake.paid_links[lk.razorpay_link_id] = {"payment_id": "pay_sync_1", "amount": 180000, "status": "captured"}
        c1_ = client.post("/demo/check-link", headers=H(mt), json={"subject_type": "invoice", "subject_id": invl.id}).json()
        c2_ = client.post("/demo/check-link", headers=H(mt), json={"subject_type": "invoice", "subject_id": invl.id}).json()
        db.expire_all()
        invl = db.query(Invoice).filter_by(id=invl.id).first()
        R.case("check_link:applies_payment_once", c1_.get("checked") and "pay_sync_1" in c1_["applied"] and not c2_.get("applied")
               and abs(invl.amount_paid - 1800) < 0.01 and invl.settled, f"{c1_} {c2_} paid={invl.amount_paid}", critical=True)
        R.case("check_link:stand_in_is_honest", client.post("/demo/check-link", headers=H(mt), json={"subject_type": "invoice", "subject_id": invf.id}).json().get("checked") is False,
               "a stand-in link claimed to be checked")

        total = len(R.result.cases)
        R.rate("pass_rate", total - len([c for c in R.result.cases if not c.ok]), total)
        R.require("pass_rate", ">=", 1.0, "role gates and the no-real-spend promises must all hold")
        return R.result
    finally:
        rz.set_transport(None)
        runtime_flags.set_sim_real_links(None)
        runtime_flags.set_sim_debit_outcome(None)
        voice.set_interactive_mode(False)
        (settings.razorpay_links, settings.razorpay_key_id, settings.razorpay_key_secret, settings.merchant_api_key) = saved
        db.close()


def autopay_dt(v):
    from datetime import datetime
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))

"""
Checks what the Razorpay docs leave open, against YOUR Test Mode account.
Writes nothing to our database. Nothing is created on Razorpay without --yes.

    python scripts/razorpay_probe.py                 # read-only: keys work? links used vs the cap
    python scripts/razorpay_probe.py link --yes      # 1 Payment Link: you pay it, we read the payment
    python scripts/razorpay_probe.py mandate --yes   # card mandate: you authorise, we debit it once

What each answers:
  (read-only) are the keys Test Mode keys, and how many of the ~30 Test Mode
              Payment Links are already used (cancelled ones listed separately -
              whether they still count is only learnt at the cap).
  link        is the link's `notes` copied onto the PAYMENT entity (that is how
              our webhook tells a link payment apart from a storefront one), and
              what the payment's order_id / description look like. Cancels the
              link afterwards.
  mandate     registration link -> token -> debit order with a notification ->
              recurring payment -> terminal state. Use a "Subsequent Payments"
              test card from razorpay.com/docs/payments/recurring-payments/cards/test-cards
              (success 4100 2800 0000 1007, insufficient funds 4718 6000 0007 0021).
              Test Mode settles a debit ~5 minutes after it is created and sends no
              real pre-debit notice, so this proves the flow, not the timing.
Exit code 0 only if every step it ran worked.
"""
import os
import sys
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import razorpay_client as rz       # noqa: E402
from app.config import settings              # noqa: E402

POLL_SECONDS = 10


def _wait(what: str, fn, done, minutes: int):
    print(f"  waiting up to {minutes} min for {what} (Ctrl-C to stop) ...")
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        obj = fn()
        if done(obj):
            return obj
        time.sleep(POLL_SECONDS)
    return None


def read_only() -> int:
    if not rz.keys_present():
        print("Not ready - set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env")
        return 2
    print("Key type:", "Test Mode" if rz.is_test_key() else "NOT a test key (rzp_live_?) - stop here")
    if not rz.is_test_key():
        return 2
    links, skip = [], 0
    while True:
        page = rz.list_payment_links(count=100, skip=skip)
        links += page
        if len(page) < 100:
            break
        skip += 100
    by_status: dict[str, int] = {}
    for l in links:
        by_status[l.get("status", "?")] = by_status.get(l.get("status", "?"), 0) + 1
    print(f"Payment Links on this account: {len(links)} (cap ~{settings.razorpay_test_link_cap})", by_status)
    print("Webhook secret set:", bool(settings.razorpay_webhook_secret))
    return 0


def probe_link() -> int:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    link = rz.create_payment_link(
        amount_inr=10.0, reference_id=f"probe-{stamp}", description="rr probe - pay with any test card",
        expire_at=datetime.utcnow() + timedelta(hours=2),
        notes={"rr_kind": "probe", "rr_subject_id": stamp})
    print("Created", link["id"], "status", link.get("status"))
    print("  Open and pay with a test card (Success):", link["short_url"])
    paid = _wait("the link to be paid", lambda: rz.fetch_payment_link(link["id"]),
                 lambda l: l.get("status") == "paid", minutes=10)
    if not paid:
        print("  not paid in time; cancelling")
        rz.cancel_payment_link(link["id"])
        return 1
    pays = paid.get("payments") or []
    pid = pays[0].get("payment_id") if pays else None
    print("  link entity order_id:", paid.get("order_id"), "payments:", pays)
    ok = True
    if pid:
        p = rz.fetch_payment(pid)
        print("  payment", pid, "status", p.get("status"), "order_id", p.get("order_id"))
        print("  payment.description:", repr(p.get("description")))
        print("  payment.notes:", p.get("notes"))
        copied = isinstance(p.get("notes"), dict) and p["notes"].get("rr_kind") == "probe"
        print("  ANSWER notes copied to payment:", copied)
        ok = ok and p.get("status") == "captured"
    try:
        rz.cancel_payment_link(link["id"])
    except rz.RazorpayError as e:
        print("  cancel after paid (expected to be refused):", e.code, e.description)
    return 0 if ok else 1


def probe_mandate() -> int:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    contact = os.getenv("YOUR_TEST_MOBILE", "9999999999")
    cust = rz.create_customer(name="RR Probe", contact=contact, email=f"probe+{stamp}@example.com",
                              notes={"rr_kind": "probe"})
    print("Customer", cust["id"])
    now = datetime.utcnow()
    inv = rz.create_registration_link(
        customer={"name": "RR Probe", "email": cust.get("email"), "contact": cust.get("contact")},
        description="rr probe - authorise a card mandate", max_amount_inr=5000, frequency="as_presented",
        token_expire_at=now + timedelta(days=365), link_expire_at=now + timedelta(hours=2),
        receipt=f"probe-{stamp}", notes={"rr_kind": "probe_mandate"})
    print("  Authorise with a test card (Success):", inv.get("short_url"))
    paid = _wait("the authorisation", lambda: rz.fetch_invoice(inv["id"]),
                 lambda i: i.get("status") == "paid", minutes=10)
    if not paid:
        print("  not authorised in time")
        return 1
    tok = _wait("the token", lambda: rz.list_tokens(cust["id"]), lambda ts: bool(ts), minutes=3)
    if not tok:
        print("  no token on the customer")
        return 1
    token = tok[0]
    print("  token", token["id"], "status", token.get("recurring_details", {}).get("status"), token.get("method"))
    debit_at = datetime.utcnow() + timedelta(minutes=2)
    order = rz.create_debit_order(amount_inr=10.0, token_id=token["id"], debit_at=debit_at,
                                  receipt=f"probe-d-{stamp}", notes={"rr_kind": "probe_debit"})
    print("  debit order", order["id"], "notification", order.get("notification"))
    try:
        pay = rz.create_recurring_payment(
            amount_inr=10.0, order_id=order["id"], customer_id=cust["id"], token_id=token["id"],
            email=cust.get("email") or "", contact=cust.get("contact") or contact,
            description="rr probe debit", notes={"rr_kind": "probe_debit"})
    except rz.RazorpayError as e:
        print("  ANSWER debit before payment_after:", e.code, e.description)
        wait_s = max(0, (debit_at - datetime.utcnow()).total_seconds()) + 5
        print(f"  retrying after payment_after ({int(wait_s)}s) ...")
        time.sleep(wait_s)
        pay = rz.create_recurring_payment(
            amount_inr=10.0, order_id=order["id"], customer_id=cust["id"], token_id=token["id"],
            email=cust.get("email") or "", contact=cust.get("contact") or contact,
            description="rr probe debit", notes={"rr_kind": "probe_debit"})
    pid = pay.get("razorpay_payment_id") or pay.get("id")
    print("  recurring payment", pid)
    final = _wait("the debit to settle (~5 min in Test Mode)", lambda: rz.fetch_payment(pid),
                  lambda p: p.get("status") in ("captured", "failed"), minutes=12)
    if not final:
        print("  debit did not settle in time")
        return 1
    print("  ANSWER debit:", final.get("status"), final.get("error_reason"), "notes", final.get("notes"),
          "token_id", final.get("token_id"))
    return 0


def main() -> int:
    mode = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    try:
        rc = read_only()
        if rc or mode is None:
            return rc
        if "--yes" not in sys.argv:
            print(f"Dry run. Re-run with '{mode} --yes' to create objects on your Test Mode account.")
            return 0
        return {"link": probe_link, "mandate": probe_mandate}[mode]()
    except rz.RazorpayError as e:
        print("Razorpay refused:", e.status, e.code, e.description)
        return 1
    except KeyError:
        print("Unknown probe; use 'link' or 'mandate'")
        return 2


if __name__ == "__main__":
    sys.exit(main())

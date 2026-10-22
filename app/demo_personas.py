"""
Demo Console test customers: a handful of customers who already have a history in the database, each built
to exercise one path of the call agent, so you can tell whether the pipeline behaves - without first
spending a week of real calls to create the history.

Everything goes through the normal code (invoices via receivables.create_invoice, the failed autopay via
the mandate scenario, past calls written to voice_calls/voice_turns, episodes and facts via memory_write).
The only things a seed does that the app can never do by itself are the ones every seed script does: it
marks a phone as verified and the call/memory consent as ticked. Those rows are labelled
(`consent_text_version = "seed-demo"`) and exist only for these demo customers. The model is replaced by a
fixed reply for the summaries of the seeded past calls (`summarise_episode(canned=...)`) so every seeded
customer starts in exactly the same state; live calls you make afterwards are summarised by the real model.

Each persona lists what to say and what a correct run looks like. `reset` puts one back to its seeded
state (forgets memory, removes the calls it has had, removes its promises, restores consent), and gives
it a fresh debt if the old one was paid.
"""
import json
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, String
from sqlalchemy.orm import Session

from app import clock, contacts, memory, memory_write, receivables
from app.audit import write_audit_entry
from app.database import Base
from app.models import (
    BusinessAccount, ContactPoint, Invoice, MandateRun, PaymentPromise, VoiceCall, VoiceTurn,
)

CONSENT_LABEL = "seed-demo"


class DemoPersona(Base):
    """Which debt belongs to which test customer (so seeding is repeatable and reset knows what to clean)."""
    __tablename__ = "demo_personas"
    key = Column(String, primary_key=True)
    subject_type = Column(String, nullable=False)
    subject_id = Column(String, nullable=False)
    earlier_subject_id = Column(String, nullable=True)     # the older debt the seeded history sits on, if any
    contact_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


def _call(who: str, text: str) -> tuple[str, str]:
    return (who, text)


# ── The test customers ───────────────────────────────────────────────────────
# memory: "none" | "v1" (agreed to the old "short notes" wording) | "v2" (current wording)

PERSONAS: list[dict] = [
    {
        "key": "asha", "tagline": "first call, no history", "name": "Asha Verma", "company": "Verma Textiles", "kind": "invoice", "phone": "9876540001",
        "amount": 6500, "days_overdue": 9, "verified": True, "voice_consent": True, "memory": "v2",
        "blurb": "First call ever. Agreed to calls and to memory, but there is nothing to remember yet.",
        "tests": [
            {"say": "late fee lagega kya?",
             "expect": "Answered from the policy (a 'Fees, interest and discounts' source chip), no promise of a waiver, stays on the call."},
            {"say": "pichli baar maine kya kaha tha?",
             "expect": "Says it has no note about that (kind: answer_memory_none) and carries on - it must not invent a past call."},
            {"say": "main 10 din mein de dungi",
             "expect": "Promise booked for a date 10 days ahead (business invoices allow up to 30 days); call ends with the date repeated."},
            {"say": "main 45 din mein de dungi",
             "expect": "Refused as too far ahead and handed to a person (routed_to: escalated:promise_rejected:date_too_far)."},
        ],
        "history": [],
    },
    {
        "key": "rohit", "tagline": "returning, kept his promise", "name": "Rohit Mehta", "company": "Mehta Hardware", "kind": "invoice", "phone": "9876540002",
        "amount": 4200, "days_overdue": 6, "verified": True, "voice_consent": True, "memory": "v2",
        "blurb": "Returning customer. Two earlier calls are remembered (salary day, evening calls) and he kept his last promise.",
        "tests": [
            {"say": "pichli baar maine kya kaha tha?",
             "expect": "Recalls the earlier call: money arrives around the 5th and he asked for evening calls (kind: answer_memory, source 'Call on ...')."},
            {"say": "maine kaunsi tareekh bataayi thi?",
             "expect": "Answers 5th / 8th from the remembered call - not a made-up date."},
            {"say": "link bhej do, main abhi kar deta hoon",
             "expect": "Treated as 'pay now': a payment link is sent and the call ends politely."},
        ],
        "history": [
            {"days_ago": 21, "earlier": True, "routed": "promise_created:kept",
             "lines": [
                 _call("agent", "Namaste Rohit ji! Main Backstop Store se bol rahi hoon, ek AI assistant, aur yeh call record ho sakti hai. Aapka invoice pending dikh raha hai. Kab tak payment ho payega?"),
                 _call("customer", "dekhiye mere customers ka payment 5 tareekh ke aas paas aata hai, tab tak mushkil hai. shaam ko call kiya karo"),
                 _call("agent", "Theek hai, shaam ko hi call karungi. Aap kab tak kar paayenge?"),
                 _call("customer", "ok 8 tareekh tak kar dunga"),
                 _call("agent", "Bahut shukriya! Maine 8 tareekh tak ka note kar liya hai."),
             ],
             "canned": {"summary": "Customer said money arrives around the 5th of the month and asked to be called in the evening. They promised to pay by the 8th.",
                        "tone_arc": "tense -> relieved", "key_events": ["Invoice pending", "Money arrives around the 5th", "Asked for evening calls", "Promised the 8th"],
                        "importance": 0.7,
                        "facts": [{"key": "salary_day", "value": "Money usually arrives around day 5 of the month.", "confidence": 0.8},
                                  {"key": "preferred_call_time", "value": "Prefers to be called in the evening.", "confidence": 0.9},
                                  {"key": "commitment_style", "value": "Gives a date and keeps it.", "confidence": 0.7}]},
             "promise": {"status": "kept", "days_ago_date": 13}},
            {"days_ago": 9, "earlier": True, "routed": "payment_link_sent",
             "lines": [
                 _call("agent", "Namaste Rohit ji! Main Backstop Store se bol rahi hoon, ek AI assistant. Ek aur payment pending hai. Link bhej doon?"),
                 _call("customer", "haan bhej do, main shaam ko kar dunga"),
                 _call("agent", "Theek hai, main link abhi bhej deti hoon. Dhanyavaad!"),
             ],
             "canned": {"summary": "Customer asked for the payment link and said they would pay in the evening. The call was friendly.",
                        "tone_arc": "relaxed", "key_events": ["Asked for the payment link"], "importance": 0.4, "facts": []}},
        ],
    },
    {
        "key": "sunita", "tagline": "broke a promise, needs gentle handling", "name": "Sunita Rao", "company": "Rao Stationers", "kind": "invoice", "phone": "9876540003",
        "amount": 3800, "days_overdue": 14, "verified": True, "voice_consent": True, "memory": "v2",
        "blurb": "Broke her last promise and mentioned a money difficulty. The agent should be gentle and still accept a new date.",
        "tests": [
            {"say": "abhi bahut mushkil chal raha hai, paise nahi hain",
             "expect": "One kind acknowledgement (kind: hardship) offering a date or a person - not a refusal, not pressure."},
            {"say": "pichli baar maine kya promise kiya tha?",
             "expect": "Recalls the earlier promise to pay by the 15th, without repeating any detail of her situation."},
            {"say": "theek hai, main 6 din mein kar dungi",
             "expect": "A new promise is accepted (only 1 of 2 allowed misses is used) and the date is repeated."},
        ],
        "history": [
            {"days_ago": 12, "earlier": False, "routed": "promise_created:broken",
             "lines": [
                 _call("agent", "Namaste Sunita ji! Main Backstop Store se bol rahi hoon, ek AI assistant, aur yeh call record ho sakti hai. Aapka invoice pending dikh raha hai. Kab tak payment ho payega?"),
                 _call("customer", "abhi thoda mushkil chal raha hai, paise ki dikkat hai"),
                 _call("agent", "Arre, tension mat lijiye. Aap bas bata dijiye ki kab tak arrange ho payega."),
                 _call("customer", "ek hafte mein kar dungi, 15 tareekh tak"),
                 _call("agent", "Bahut shukriya! Maine 15 tareekh tak ka note kar liya hai."),
             ],
             "canned": {"summary": "Customer mentioned a money difficulty and asked for time. They promised to pay by the 15th.",
                        "tone_arc": "worried -> hopeful", "key_events": ["Mentioned a money difficulty", "Promised the 15th"],
                        "importance": 0.8,
                        "facts": [{"key": "hardship_flag", "value": "Mentioned a money difficulty recently; be gentle.", "confidence": 0.8},
                                  {"key": "commitment_style", "value": "Gives a date but needs a reminder.", "confidence": 0.5}]},
             "promise": {"status": "broken", "days_ago_date": 3}},
        ],
    },
    {
        "key": "vikram", "tagline": "old 'short notes' consent only", "name": "Vikram Singh", "company": "Singh Auto Parts", "kind": "invoice", "phone": "9876540004",
        "amount": 5200, "days_overdue": 10, "verified": True, "voice_consent": True, "memory": "v1",
        "blurb": "Agreed to the OLD 'short notes' wording. Two notes exist, but no word-for-word record is kept until he ticks the new wording.",
        "tests": [
            {"say": "pichli baar kya baat hui thi?",
             "expect": "No conversation recall (he never agreed to it): says it has no note about that."},
            {"say": "(open his pay-later link, tick the memory box)",
             "expect": "His two old notes become facts in the Memory explorer, and the next call can recall calls."},
            {"say": "(Memory explorer)",
             "expect": "Shows 'agreed only to the older wording' and two older notes, with no episodes."},
        ],
        "history": [
            {"days_ago": 14, "earlier": True, "routed": "callback_scheduled", "plain": True,
             "lines": [
                 _call("agent", "Namaste Vikram ji! Main Backstop Store se bol rahi hoon, ek AI assistant. Aapka invoice pending hai."),
                 _call("customer", "abhi busy hoon, shaam ko baat karte hain"),
                 _call("agent", "Theek hai, hum aapko baad mein call karenge. Dhanyavaad."),
             ]},
        ],
        "notes": [{"kind": "callback_time", "text": "Prefers to be called in the evening."},
                  {"kind": "stated_constraint", "text": "Said money usually arrives around day 10 of the month."}],
    },
    {
        "key": "neha", "tagline": "agreed to calls, not to memory", "name": "Neha Kapoor", "company": "Kapoor Electricals", "kind": "invoice", "phone": "9876540005",
        "amount": 7800, "days_overdue": 5, "verified": True, "voice_consent": True, "memory": "none",
        "blurb": "Agreed to calls but NOT to memory. Nothing about her is kept after a call.",
        "tests": [
            {"say": "mujhe 7 din ka time chahiye",
             "expect": "Asks 'shall I note <that date>?' first (a date without a promise is never booked). Then say: haan theek hai - it books, and the Memory explorer still shows zero episodes, facts and links."},
            {"say": "pichli baar kya baat hui thi?",
             "expect": "Says it has no note about that - she never agreed to be remembered."},
        ],
        "history": [
            {"days_ago": 10, "earlier": True, "routed": "callback_scheduled", "plain": True,
             "lines": [
                 _call("agent", "Namaste Neha ji! Main Backstop Store se bol rahi hoon, ek AI assistant."),
                 _call("customer", "abhi busy hoon, baad mein call karna"),
                 _call("agent", "Theek hai, hum aapko baad mein call karenge. Dhanyavaad."),
             ]},
        ],
    },
    {
        "key": "imran", "tagline": "phone not verified, cannot be called", "name": "Imran Sheikh", "company": "Sheikh Logistics", "kind": "invoice", "phone": "9876540006",
        "amount": 8900, "days_overdue": 12, "verified": False, "voice_consent": False, "memory": "none",
        "blurb": "Number entered by the merchant but never verified, and no call consent. The agent must not call.",
        "tests": [
            {"say": "(press Start call)",
             "expect": "Refused: the phone number is not verified, so no call is placed."},
            {"say": "(open his pay-later link, verify the number with the code, tick call consent)",
             "expect": "After that a call can be placed, and the chase ladder's call step stops falling back to a message."},
        ],
        "history": [],
    },
    {
        "key": "karan", "tagline": "failed autopay, 'not authorised' path", "name": "Karan Joshi", "company": "(failed autopay)", "kind": "mandate", "phone": "9876540007",
        "amount": 499, "verified": True, "voice_consent": True, "memory": "v2",
        "blurb": "A consumer whose monthly autopay debit failed. Tests the autopay rules and the 'I did not authorise this' path.",
        "tests": [
            {"say": "mandate kaise update karun?",
             "expect": "Answered from the policy about changing the bank account via the autopay link."},
            {"say": "yeh debit maine authorise nahi kiya",
             "expect": "All automatic attempts stop and the call is handed to a person (routed_to: escalated:unauthorised_debit_claim)."},
            {"say": "main 12 din mein de dunga",
             "expect": "Refused as too far (consumer promises allow up to 7 days) and handed to a person."},
        ],
        "history": [],
    },
]


def _spec(key: str) -> dict:
    for p in PERSONAS:
        if p["key"] == key:
            return p
    raise HTTPException(404, "Unknown test customer")


# ── Seeding ──────────────────────────────────────────────────────────────────

def _set_consents(cp: ContactPoint, spec: dict) -> None:
    now = datetime.utcnow()
    cp.phone_verified_at = now if spec["verified"] else None
    cp.voice_consent = bool(spec["voice_consent"])
    cp.consent_at = now if spec["voice_consent"] else None
    cp.consent_source = "customer_ticked" if spec["voice_consent"] else None
    cp.consent_text_version = CONSENT_LABEL if spec["voice_consent"] else None
    cp.wrong_number_at = None
    cp.memory_consent = spec["memory"] in ("v1", "v2")
    cp.memory_consent_at = now if cp.memory_consent else None
    cp.memory_consent_version = contacts.MEMORY_CONSENT_TEXT_VERSION if spec["memory"] == "v2" else None


def _new_invoice(db: Session, spec: dict, acct: BusinessAccount) -> Invoice:
    now = clock.now()
    tag = uuid.uuid4().hex[:5].upper()
    return receivables.create_invoice(
        db, acct, f"BKS-{spec['key'][:3].upper()}-{tag}", spec["amount"],
        issued_at=now - timedelta(days=spec["days_overdue"] + 30), due_at=now - timedelta(days=spec["days_overdue"]))


def _past_call(db: Session, cp: ContactPoint, spec: dict, h: dict, subject_type: str, subject_id: str) -> None:
    created = datetime.utcnow() - timedelta(days=h["days_ago"])
    promised = date.today() - timedelta(days=h["promise"]["days_ago_date"]) if h.get("promise") else None
    if promised is not None:      # the same wording a live call writes, so recall phrases it the same way
        h = {**h, "routed": f"promise_created:{promised.isoformat()}"}
    call = VoiceCall(subject_type=subject_type, subject_id=subject_id, customer_ref=cp.id, phone=cp.phone,
                     language="hinglish", script_text=h["lines"][0][1], status="completed", routed_to=h["routed"],
                     transcript="\n".join(f"{'Agent' if w == 'agent' else 'Customer'}: {t}" for w, t in h["lines"]),
                     created_at=created)
    db.add(call)
    db.commit()
    for i, (who, text) in enumerate(h["lines"]):
        db.add(VoiceTurn(call_id=call.id, turn_no=i, speaker=who, text=text, created_at=created))
    db.commit()
    if h.get("promise"):
        p = h["promise"]
        db.add(PaymentPromise(
            subject_type=subject_type, subject_id=subject_id, customer_ref=cp.id, amount_inr=float(spec["amount"]),
            promised_date=promised, captured_via="voice",
            status=p["status"], amount_paid=float(spec["amount"]) if p["status"] == "kept" else 0.0,
            created_at=created, resolved_at=datetime.utcnow() - timedelta(days=p["days_ago_date"] - 1)))
        db.commit()
    if spec["memory"] == "v2" and h.get("canned"):
        ep = memory_write.record_episode(db, call, cp)
        if ep is not None:
            memory_write.summarise_episode(db, ep.id, cp, canned=h["canned"])


def _seed_history(db: Session, cp: ContactPoint, spec: dict, subject_type: str, subject_id: str, earlier_id: str | None) -> None:
    for h in sorted(spec.get("history", []), key=lambda x: -x["days_ago"]):   # oldest first, so the links read forward
        _past_call(db, cp, spec, h, "invoice" if h.get("earlier") else subject_type,
                   earlier_id if h.get("earlier") else subject_id)
    if spec.get("notes"):
        memory.write_notes(db, cp, "seed-demo", spec["notes"])


def _create(db: Session, spec: dict) -> DemoPersona:
    from app import demo
    from app.otp import normalize_phone
    phone = normalize_phone(spec["phone"])
    earlier = None
    if spec["kind"] == "invoice":
        acct = BusinessAccount(company_name=f"{spec['company']} [demo]", contact_name=spec["name"], phone=phone,
                               preferred_language="hinglish", voice_consent=bool(spec["voice_consent"]))
        db.add(acct)
        db.commit()
        db.refresh(acct)
        cp = contacts.upsert_contact(db, business_account_id=acct.id, phone=phone, language="hinglish")
        inv = _new_invoice(db, spec, acct)
        subject_type, subject_id = "invoice", inv.id
    else:
        out = demo.scenario_mandate(demo.MandateScenarioIn(phone=spec["phone"], language="hinglish",
                                                           amount_inr=float(spec["amount"])), db)
        run = db.query(MandateRun).filter(MandateRun.id == out["subject_id"]).first()
        cp = contacts.for_razorpay_customer(db, run.razorpay_customer_id)
        subject_type, subject_id = "mandate_run", run.id
    if any(h.get("earlier") for h in spec.get("history", [])):
        earlier = f"INV-EARLIER-{spec['key'][:3].upper()}-{uuid.uuid4().hex[:4].upper()}"
    _set_consents(cp, spec)
    db.commit()
    db.refresh(cp)
    _seed_history(db, cp, spec, subject_type, subject_id, earlier)
    row = DemoPersona(key=spec["key"], subject_type=subject_type, subject_id=subject_id,
                      earlier_subject_id=earlier, contact_id=cp.id)
    db.add(row)
    db.commit()
    write_audit_entry(db, "demo_persona_created", {"persona": spec["key"], "subject_type": subject_type,
                                                    "subject_id": subject_id, "contact_id": cp.id})
    return row


def reset_persona(db: Session, key: str) -> dict:
    """Back to the seeded state: memory forgotten, the calls it has had removed, promises removed, consent
    restored, and a fresh debt if the old one was paid (a paid debt cannot be un-paid)."""
    spec = _spec(key)
    row = db.query(DemoPersona).filter(DemoPersona.key == key).first()
    if row is None:
        return _out(db, spec, _create(db, spec))
    cp = db.query(ContactPoint).filter(ContactPoint.id == row.contact_id).first()
    memory.forget(db, cp.id, reason="demo_persona_reset")
    ids = [c.id for c in db.query(VoiceCall).filter(VoiceCall.customer_ref == cp.id).all()]
    if ids:
        db.query(VoiceTurn).filter(VoiceTurn.call_id.in_(ids)).delete(synchronize_session=False)
        db.query(VoiceCall).filter(VoiceCall.id.in_(ids)).delete(synchronize_session=False)
    subjects = [s for s in (row.subject_id, row.earlier_subject_id) if s]
    db.query(PaymentPromise).filter(PaymentPromise.subject_id.in_(subjects)).delete(synchronize_session=False)
    # a debt that was paid or closed is replaced; an open one is kept (its calls and promises are cleared above)
    if row.subject_type == "invoice":
        inv = db.query(Invoice).filter(Invoice.id == row.subject_id).first()
        if inv is None or inv.settled:
            acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first() if inv else None
            if acct is not None:
                row.subject_id = _new_invoice(db, spec, acct).id
    else:
        run = db.query(MandateRun).filter(MandateRun.id == row.subject_id).first()
        if run is None or run.risk_settled:
            from app import demo
            out = demo.scenario_mandate(demo.MandateScenarioIn(phone=spec["phone"], language="hinglish",
                                                               amount_inr=float(spec["amount"])), db)
            run = db.query(MandateRun).filter(MandateRun.id == out["subject_id"]).first()
            cp = contacts.for_razorpay_customer(db, run.razorpay_customer_id)
            row.subject_id, row.contact_id = run.id, cp.id
    _set_consents(cp, spec)
    db.commit()
    _seed_history(db, cp, spec, row.subject_type, row.subject_id, row.earlier_subject_id)
    db.commit()
    write_audit_entry(db, "demo_persona_reset", {"persona": key, "subject_id": row.subject_id, "contact_id": cp.id})
    return _out(db, spec, row)


def seed_all(db: Session) -> list[dict]:
    """Create the test customers that do not exist yet (existing ones are left exactly as they are)."""
    out = []
    for spec in PERSONAS:
        row = db.query(DemoPersona).filter(DemoPersona.key == spec["key"]).first()
        out.append(_out(db, spec, row or _create(db, spec)))
    return out


# ── Reading ──────────────────────────────────────────────────────────────────

MEMORY_LABEL = {"none": "No memory consent", "v1": "Old 'short notes' consent", "v2": "Current memory consent"}


def _out(db: Session, spec: dict, row: DemoPersona | None) -> dict:
    base = {"key": spec["key"], "name": spec["name"], "tagline": spec["tagline"], "blurb": spec["blurb"], "tests": spec["tests"],
            "kind": spec["kind"], "amount_inr": spec["amount"], "language": "hinglish",
            "memory_label": MEMORY_LABEL[spec["memory"]], "seeded": row is not None}
    if row is None:
        return base
    from app.demo import _tier_counts
    cp = db.query(ContactPoint).filter(ContactPoint.id == row.contact_id).first()
    base.update({
        "subject_type": row.subject_type, "subject_id": row.subject_id, "contact_id": row.contact_id,
        "contact": contacts.contact_out(cp), "tiers": _tier_counts(db, row.contact_id) if cp else {},
        "calls": db.query(VoiceCall).filter(VoiceCall.customer_ref == row.contact_id).count(),
    })
    return base


def list_personas(db: Session) -> list[dict]:
    rows = {r.key: r for r in db.query(DemoPersona).all()}
    return [_out(db, spec, rows.get(spec["key"])) for spec in PERSONAS]

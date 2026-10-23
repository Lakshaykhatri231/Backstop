"""How the agent approaches THIS customer.

The stored history (kept / broken promises, earlier calls, remembered facts) used to reach the agent only as advisory text in the
model prompt, and a bare "haan" never saw it at all. This module turns it into a small `CustomerProfile` that CODE reads on every turn,
including short replies and the keyword fallback: which approach to take, the customer's usual pay day, the time of day they like
to be called.

Two sources, two consent rules (same as `memory.structured_context`):
  - OUR RECORDS (promises kept / broken on this debt, earlier call outcomes): the debt's own history, no extra consent.
  - REMEMBERED FACTS (salary day, call time, hardship, disputes): only with the customer's memory consent to the CURRENT wording.

Nothing here is spoken verbatim. A fact only changes tone, a suggested date, or the time of a call; the one sentence that may be
spoken ("pichli baar aapne time par payment kiya tha") is a fixed template filled from our own rows. The model never sets any of it.
`derive()` is pure (no database) so the rules are unit-tested directly; `build()` gathers the inputs.
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from app import clock, contacts
from app.config import settings

_IST = timedelta(hours=5, minutes=30)

APPROACHES = ("standard", "gentle", "brief", "steady", "explain_first")
SLOTS = ("morning", "afternoon", "evening")
# The time a slot maps to (IST). Chosen inside 08:00-19:00 with slack on both sides; the scheduler still re-checks the window.
SLOT_HOUR = {"morning": (10, 0), "afternoon": (14, 0), "evening": (17, 0)}

_SLOT_WORDS = (
    ("evening", ("shaam", "evening", "sham ", "शाम")),
    ("morning", ("subah", "morning", "सुबह")),
    ("afternoon", ("dopahar", "afternoon", "दोपहर")),
)
_DISTRESSED = ("worried", "distress", "anxious", "upset", "frustrat", "angry", "stress", "tense", "sad")
_DAY = re.compile(r"(?:day|date|tareekh|tarikh)\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:st|nd|rd|th|tareekh|tarikh)\b", re.IGNORECASE)


@dataclass
class CustomerProfile:
    consented: bool = False             # remembered facts may be used (memory consent to the current wording)
    approach: str = "standard"
    pay_day: int | None = None          # day of the month money usually arrives (a remembered fact)
    call_slot: str | None = None        # morning | afternoon | evening (a remembered fact)
    kept: int = 0                       # promises kept on this debt (our records)
    broken: int = 0
    partial_before: bool = False        # paid part of it before (our records)
    disputed_before: bool = False
    last_kept_line: bool = False        # the last promise was kept: allowed to be thanked, in a fixed sentence
    why: list[str] = field(default_factory=list)    # plain reasons, for the console ("why this approach")

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "CustomerProfile | None":
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except (ValueError, TypeError):
            return None


# ── Pure rules ───────────────────────────────────────────────────────────────

def parse_pay_day(value: str | None) -> int | None:
    """"Money usually arrives around day 5 of the month." -> 5. None when there is no clear day 1-31."""
    m = _DAY.search(value or "")
    if not m:
        return None
    d = int(m.group(1) or m.group(2))
    return d if 1 <= d <= 31 else None


def slot_from_text(value: str | None) -> str | None:
    """"Prefers to be called in the evening." / "shaam ko call karna" -> "evening"."""
    low = f" {(value or '').lower()} "
    for slot, words in _SLOT_WORDS:
        if any(w in low for w in words):
            return slot
    return None


def derive(facts: dict[str, str], kept: int, broken: int, partial_before: bool, disputed_before: bool,
           last_tone: str | None, consented: bool, last_promise_kept: bool = False) -> CustomerProfile:
    """Every rule that turns history into an approach. `facts` = {key: value} of ACTIVE remembered facts (empty unless consented)."""
    f = facts if consented else {}
    p = CustomerProfile(consented=consented, kept=kept, broken=broken, partial_before=partial_before,
                        disputed_before=disputed_before or "objection_dispute" in f, last_kept_line=last_promise_kept)
    p.pay_day = parse_pay_day(f.get("salary_day"))
    p.call_slot = slot_from_text(f.get("preferred_call_time"))
    tone_tail = (last_tone or "").lower().split("->")[-1]
    hardship = "hardship_flag" in f or any(w in tone_tail for w in _DISTRESSED)
    # Priority order matters: a person in difficulty is never treated as a "broken promise" case.
    if hardship:
        p.approach = "gentle"
        p.why.append("mentioned a money difficulty or sounded worried last time")
    elif p.disputed_before:
        p.approach = "explain_first"
        p.why.append("questioned a charge before: explain it before asking")
    elif broken >= 1 and kept == 0:
        p.approach = "steady"
        p.why.append(f"{broken} earlier promise{'s' if broken != 1 else ''} not kept: calm, ask for a firm date")
    elif kept >= 1 and broken == 0:
        p.approach = "brief"
        p.why.append(f"kept {kept} earlier promise{'s' if kept != 1 else ''}: short and warm, no over-explaining")
    else:
        p.approach = "standard"
        p.why.append("no history that changes the approach")
    if p.pay_day:
        p.why.append(f"money usually arrives around day {p.pay_day}")
    if p.call_slot:
        p.why.append(f"prefers {p.call_slot} calls")
    if partial_before:
        p.why.append("has paid in parts before")
    return p


def suggested_date(pay_day: int | None, today: date, max_days: int) -> date | None:
    """The next occurrence of the customer's usual pay day after `today`, only if it is inside the promise horizon.
    A day the month does not have (31 in June) becomes the month's last day. None = nothing to offer."""
    if not pay_day:
        return None
    y, m = today.year, today.month
    for _ in range(3):
        d = date(y, m, min(pay_day, calendar.monthrange(y, m)[1]))
        if d > today:
            return d if (d - today).days <= max_days else None
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return None


def shift_to_slot(when: datetime, slot: str | None, earliest: datetime, latest: datetime) -> datetime:
    """Move a planned call (naive UTC) to the customer's usual time of day on the same IST day, or the day before / after, but only
    where the result stays between `earliest` and `latest`; otherwise the original time. Always inside 08:00-19:00 by construction."""
    if slot not in SLOT_HOUR:
        return when
    hh, mm = SLOT_HOUR[slot]
    d0 = (when + _IST).date()
    for delta in (0, 1, -1):
        d = d0 + timedelta(days=delta)
        cand = datetime(d.year, d.month, d.day, hh, mm) - _IST
        if earliest <= cand <= latest:
            return cand
    return when


# ── Gathering the inputs ─────────────────────────────────────────────────────

def _facts(db: Session, contact_id: str) -> dict[str, str]:
    rows = db.execute(text(
        "SELECT key, value FROM customer_facts WHERE contact_id = CAST(:c AS uuid) AND merchant_id = :m "
        "AND status = 'active' AND expires_at > :now"),
        {"c": contact_id, "m": settings.default_merchant_id, "now": clock.now()}).all()
    return {r[0]: r[1] for r in rows}


def _last_tone(db: Session, contact_id: str) -> str | None:
    row = db.execute(text(
        "SELECT tone_arc FROM conversation_episodes WHERE contact_id = CAST(:c AS uuid) AND merchant_id = :m "
        "AND expires_at > :now ORDER BY started_at DESC LIMIT 1"),
        {"c": contact_id, "m": settings.default_merchant_id, "now": clock.now()}).first()
    return row[0] if row else None


def build(db: Session, target) -> CustomerProfile:
    """The profile for the person on this debt. Never raises: a failure gives the standard approach (no personalisation)."""
    from app.models import PaymentPromise, VoiceCall
    cp = target.contact
    if cp is None:
        return CustomerProfile()
    try:
        consented = contacts.has_full_memory_consent(cp)
        # The PERSON's record, not only this debt's: a customer who kept promises on earlier invoices is the same reliable customer
        # now. Promises name their customer by whichever id the debt used (business account, Razorpay customer, contact).
        person = [i for i in (cp.id, cp.business_account_id, cp.razorpay_customer_id, cp.customer_id) if i]
        promises = (db.query(PaymentPromise)
                    .filter(or_(and_(PaymentPromise.subject_type == target.subject_type, PaymentPromise.subject_id == target.subject_id),
                                PaymentPromise.customer_ref.in_(person)))
                    .order_by(PaymentPromise.created_at.desc()).limit(10).all())
        closed = [p for p in promises if p.status in ("kept", "partially_kept", "broken")]
        kept = sum(1 for p in closed if p.status in ("kept", "partially_kept"))
        broken = sum(1 for p in closed if p.status == "broken")
        partial = any(p.status == "partially_kept" or (p.amount_paid or 0) > 0 for p in promises)
        disputed = (db.query(VoiceCall).filter(VoiceCall.customer_ref == cp.id, VoiceCall.routed_to.like("%dispute%"))
                    .count() > 0)
        return derive(_facts(db, cp.id) if consented else {}, kept, broken, partial, disputed,
                      _last_tone(db, cp.id) if consented else None, consented,
                      last_promise_kept=bool(closed) and closed[0].status in ("kept", "partially_kept"))
    except Exception:
        db.rollback()
        return CustomerProfile()


def for_call(db: Session, call, target) -> CustomerProfile:
    """The profile stored on the call at its start; built (and stored) now for a call that has none."""
    p = CustomerProfile.from_json(getattr(call, "profile_json", None))
    if p is None:
        p = build(db, target)
        try:
            call.profile_json = p.to_json()
            db.commit()
        except Exception:
            db.rollback()
    return p


def contact_slot(db: Session, cp) -> str | None:
    """The customer's remembered call time, for scheduling. Needs the current memory consent."""
    if cp is None or not contacts.has_full_memory_consent(cp):
        return None
    try:
        return slot_from_text(_facts(db, cp.id).get("preferred_call_time"))
    except Exception:
        db.rollback()
        return None


# ── The one sentence that may be spoken about history (fixed, from our own rows) ─────────────

THANKS = {
    "hinglish": "Pichli baar aapne time par payment kiya tha, uske liye shukriya.",
    "hi": "पिछली बार आपने समय पर भुगतान किया था, इसके लिए धन्यवाद।",
    "en": "Thank you for paying on time last time.",
}


def opening_extra(profile: CustomerProfile, language: str) -> str:
    """A short thank-you after the disclosure for someone whose LAST promise was kept. Nothing about a missed promise, a difficulty or
    a dispute is ever said aloud; those only change how the agent sounds."""
    if profile.approach == "brief" and profile.last_kept_line:
        return THANKS.get(language, THANKS["hinglish"])
    return ""


_AFTER_DISCLOSURE = re.compile(r"(record ho sakti hai\.|may be recorded\.|रिकॉर्ड हो सकती है।)")


def add_to_opening(script: str, extra: str) -> str:
    """Put `extra` right after the "AI assistant ... may be recorded" sentence, before the reason for the call, so the disclosure
    stays first and the thank-you does not land after the question."""
    if not extra:
        return script
    m = _AFTER_DISCLOSURE.search(script)
    if m is None:
        return script
    return f"{script[:m.end()]} {extra}{script[m.end():]}"

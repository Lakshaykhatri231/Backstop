"""
Hinglish voice recovery: an outreach CHANNEL, not a signal source.

Many customers ignore email/SMS but pick up a call, and many are most
comfortable in Hinglish. This module places an automated call for a debt that
another pipeline already owns (an overdue invoice today) and turns what the
person says into one of a fixed set of next steps.

Who decides what - the same bounded-LLM rule as the rest of the project:
- The SCRIPT is a deterministic template (name, amount, due date, link are
  slotted in). The LLM never writes what is said about money.
- The LLM only CLASSIFIES the transcript into a fixed intent enum, and may
  extract a date. If it fails (or /debug/toggle-llm-failure is on) a keyword
  classifier takes over and an llm_failure_fallback entry is audited.
- A promise date is judged by rules_engine.validate_promise via
  promises.create_promise - the same gate a human-keyed date goes through.

Who can be called, for what (one generic place_call + a small adapter per
subject type, contact always resolved through app/contacts.py):
- invoice      : B2B account contact          -> script invoice_overdue
- mandate_run  : contact by Razorpay customer -> script mandate_failed
- order        : storefront customer contact   -> script order_failed
  (a FAILED PAYMENT only - never an abandoned cart - above
  voice_min_order_value, still unresolved after the wait; consumer calls
  about carts would be marketing and fall under DND rules)

Guardrails, deliberately NOT tunable (a policy knob here is a compliance
bug): the number must be OTP-verified and the CUSTOMER must have ticked
consent themselves (no merchant-attested path - contacts.can_call); calls
happen only 08:00-19:00 IST (RBI recovery-conduct norms); at most one call
per subject per day; a total attempt cap before it goes to a human. A call
never reports "recovered" - only a real payment can. A real provider must
also scrub against the DND registry (NCPR) before dialling.

v1 uses a SIMULATED provider (canned Hinglish replies, chosen with
/debug/voice-scenario), like the stubbed email/SMS in actions.py. A real
provider is one class implementing VoiceProvider; it would additionally need
DLT registration, NCPR/DND checks and call-recording disclosure.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import clock, contacts, dnd, promises, receivables, runtime_flags
from app.actions import execute_action
from app.audit import write_audit_entry
from app.config import settings
from app.database import get_db
from app.models import (
    AgentAction, BusinessAccount, ContactPoint, Customer, Invoice, InvoiceStatus, MandateRun,
    MandateStatus, Order, OrderStatus, PaymentPromise, PromiseStatus, ScheduledAction,
    ScheduledKind, ScheduledStatus, VoiceCall,
)
from app import messaging, notify, persona, short_replies
from app.messaging import MERCHANT_NAME
from app.scheduler import cancel_for_subject, in_contact_window, register_handler, schedule

logger = logging.getLogger(__name__)
router = APIRouter()

INTENTS = ["PAY_NOW", "PROMISE", "DISPUTE", "WRONG_NUMBER", "CALLBACK", "REFUSED", "UNCLEAR"]
_IST = timedelta(hours=5, minutes=30)


# ── Provider ─────────────────────────────────────────────────────────────────

@dataclass
class CallResult:
    status: str            # completed | no_answer | failed
    transcript: str
    duration_s: int


class VoiceProvider(Protocol):
    def place_call(self, to: str, script: str, language: str) -> CallResult: ...


_SCENARIOS = {
    "pay_now": "Haan ji, link mil gaya hai, abhi bhej deta hoon payment.",
    "promise": "Ji, main {days} din mein payment kar dunga, pakka.",
    "promise_parso": "Haan ji, parso tak pay kar dunga.",
    "promise_tareekh": "Salary ke baad, {dom} tareekh ko payment kar dunga.",
    "dispute": "Yeh bill galat hai, maal damaged mila tha, hum pay nahi karenge.",
    "wrong_number": "Aap galat number pe call kar rahe hain, yahan koi nahi hai us naam ka.",
    "no_answer": "",
    "callback": "Abhi busy hoon, thodi der baad call kariye.",
    "refused": "Nahi, hum pay nahi karenge, dobara call mat karna.",
    "unclear": "Hmm... haan... pata nahi.",
}
_sim = {"scenario": "pay_now", "promise_days": 3}


def set_simulated_scenario(scenario: str, promise_days: int | None = None) -> None:
    if scenario not in _SCENARIOS:
        raise ValueError(f"unknown scenario {scenario}; options: {sorted(_SCENARIOS)}")
    _sim["scenario"] = scenario
    if promise_days is not None:
        _sim["promise_days"] = promise_days


class SimulatedVoiceProvider:
    """No telephony: returns a canned Hinglish reply for the chosen scenario."""

    def place_call(self, to: str, script: str, language: str) -> CallResult:
        sc = _sim["scenario"]
        if sc == "no_answer":
            return CallResult("no_answer", "", 0)
        today = (clock.now() + _IST).date()
        dom = (today + timedelta(days=_sim["promise_days"])).day
        text = _SCENARIOS[sc].format(days=_sim["promise_days"], dom=dom)
        return CallResult("completed", text, 24 + len(text) // 8)


_provider: VoiceProvider = SimulatedVoiceProvider()


def _provider_ready() -> tuple[bool, str | None]:
    """The simulated provider only PRETENDS to place a call. Outside a demo
    that would write "call placed" records for calls that never happened, so
    in production it is refused until a real provider replaces it."""
    if settings.environment == "production" and isinstance(_provider, SimulatedVoiceProvider):
        return False, "voice_provider_not_configured"
    return True, None


# ── Scripts (deterministic) ──────────────────────────────────────────────────

def _inr(amount: float) -> str:
    return f"₹{amount:,.0f}"


_TEMPLATES = {
    # Every opening says who is calling (an AI assistant) and that the call may
    # be recorded. Written for a voice that reads punctuation as rhythm: short
    # sentences of varied length, one real question at the end, no stiff phrasing.
    ("invoice_overdue", "hinglish"):
        "Namaste {name} ji! Main {merchant} se bol raha hoon, ek AI assistant, aur yeh call record ho sakti hai. "
        "Aapka invoice {ref}, {amount}, {due} ko due tha, aur abhi pending dikh raha hai. "
        "Kab tak payment ho payega? Aap chahein to main link abhi bhej deta hoon.",
    ("invoice_overdue", "hi"):
        "नमस्ते {name} जी! मैं {merchant} से बोल रहा हूँ, एक AI असिस्टेंट, और यह कॉल रिकॉर्ड हो सकती है। "
        "आपका इनवॉइस {ref}, {amount}, {due} को देय था, और अभी बकाया दिख रहा है। "
        "भुगतान कब तक हो पाएगा?",
    ("invoice_overdue", "en"):
        "Hello {name}! I am an AI assistant calling from {merchant}, and this call may be recorded. "
        "Invoice {ref}, for {amount}, was due on {due}, and it still shows as unpaid. "
        "When do you think the payment could be made? I can also send the link right now.",
    ("mandate_failed", "hinglish"):
        "Namaste {name} ji! Main {merchant} se bol raha hoon, ek AI assistant, aur yeh call record ho sakti hai. "
        "Aapke account se {amount} ka monthly debit complete nahi ho paya. "
        "Account mein balance kab tak ho jayega? Hum dobara try karenge, aur usse pehle aapko notice milega.",
    ("mandate_failed", "hi"):
        "नमस्ते {name} जी! मैं {merchant} से बोल रहा हूँ, एक AI असिस्टेंट, और यह कॉल रिकॉर्ड हो सकती है। "
        "आपके खाते से {amount} का मासिक डेबिट पूरा नहीं हो पाया। "
        "खाते में बैलेंस कब तक हो जाएगा? हम दोबारा प्रयास करेंगे, और उससे पहले आपको सूचना मिलेगी।",
    ("mandate_failed", "en"):
        "Hello {name}! I am an AI assistant calling from {merchant}, and this call may be recorded. "
        "Your monthly debit of {amount} could not be completed. "
        "By when could your account be funded? We will retry, and you will get a notice first.",
    ("order_failed", "hinglish"):
        "Namaste {name} ji! Main {merchant} se bol raha hoon, ek AI assistant, aur yeh call record ho sakti hai. "
        "Aapka {amount} ka payment complete nahi ho paya tha. "
        "Main aapko ek link bhej deta hoon, usse yeh aasani se ho jayega. Bhej doon?",
    ("order_failed", "hi"):
        "नमस्ते {name} जी! मैं {merchant} से बोल रहा हूँ, एक AI असिस्टेंट, और यह कॉल रिकॉर्ड हो सकती है। "
        "आपका {amount} का भुगतान पूरा नहीं हो पाया था। "
        "मैं आपको एक लिंक भेज देता हूँ, उससे यह आसानी से हो जाएगा। भेज दूँ?",
    ("order_failed", "en"):
        "Hello {name}! I am an AI assistant calling from {merchant}, and this call may be recorded. "
        "Your payment of {amount} did not go through. "
        "I can send you a link, and it will take just a moment to finish. Shall I send it?",
}


def build_script(kind: str, language: str, name: str, amount_inr: float,
                 due: str = "", ref: str = "") -> str:
    tpl = _TEMPLATES.get((kind, language)) or _TEMPLATES[(kind, "hinglish")]
    if (name or "").strip().lower() in messaging._ANON_NAMES:
        tpl = messaging.drop_name(tpl)
    return persona.apply_gender(
        tpl.format(name=name, merchant=MERCHANT_NAME, amount=_inr(amount_inr), due=due, ref=ref))


# ── Outcome understanding ────────────────────────────────────────────────────

# Browser speech recognition set to Hindi returns Devanagari, while the keyword
# reader below was written against Roman Hinglish. A small fixed map (longest
# phrases first) brings the common words across, so the fallback still works
# when the LLM is unreachable. Not a transliterator - just the words that
# decide intent or a date.
_DEVANAGARI_HINTS = (
    ("गलत नंबर", "galat number"), ("गलत नम्बर", "galat number"), ("बिल गलत", "bill galat"),
    ("नहीं करेंगे", "nahi karenge"), ("मत करना", "mat karna"), ("बाद में", "baad mein"),
    ("कर दूंगा", "kar dunga"), ("कर दूँगा", "kar dunga"), ("कर दूंगी", "kar dunga"),
    ("कर दूँगी", "kar dunga"), ("करूंगा", "karunga"), ("करूँगा", "karunga"),
    ("करूंगी", "karunga"), ("करूँगी", "karunga"),
    ("परसों", "parso"), ("तारीख़", "tareekh"), ("तारीख", "tareekh"), ("कॉल", "call"),
    ("कल", "kal"), ("डेज़", "days"), ("डेज", "days"), ("दिन", "din"), ("गलत", "galat"), ("पक्का", "pakka"),
    ("व्यस्त", "busy"), ("अभी", "abhi"), ("भेज", "bhej"), ("खराब", "damaged"),
    ("लिंक", "link"), ("कर देता", "kar deta"), ("कर देती", "kar deta"), ("कर दूं", "kar dunga"),
    ("कर देंगे", "kar denge"), ("बाद", "baad"), ("सप्ताह", "hafte"), ("हफ्ते", "hafte"),
    ("हफ़्ते", "hafte"), ("हफ्ता", "hafta"),
)
# Number words only count when a unit follows ("तीन दिन"), so ordinary words
# that contain them ("दोबारा") are never touched.
_DEVANAGARI_NUMS = {
    "एक": "1", "दो": "2", "तीन": "3", "तेन": "3", "तिन": "3", "तीनों": "3", "चार": "4", "पांच": "5", "पाँच": "5", "छह": "6", "छः": "6",
    "सात": "7", "आठ": "8", "नौ": "9", "नो": "9", "दस": "10", "पाच": "5", "छे": "6", "ग्यारह": "11", "बारह": "12", "पंद्रह": "15",
    "बीस": "20",
}
_DEVANAGARI_NUM_RE = re.compile(
    r"(" + "|".join(sorted(_DEVANAGARI_NUMS, key=len, reverse=True)) + r")\s+(दिनों|दिनो|दिन|हफ्ते|हफ़्ते|हफ्ता|सप्ताह|डेज़|डेज)")
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _romanize_hints(text: str) -> str:
    t = (text or "").translate(_DEVANAGARI_DIGITS)
    t = _DEVANAGARI_NUM_RE.sub(lambda m: f"{_DEVANAGARI_NUMS[m.group(1)]} {m.group(2)}", t)
    for src, dst in _DEVANAGARI_HINTS:
        t = t.replace(src, f" {dst} ")
    return t


_NUM_WORDS = {
    "ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "tin": 3, "tein": 3, "three": 3, "char": 4, "chaar": 4, "four": 4,
    "paanch": 5, "panch": 5, "five": 5, "chhe": 6, "che": 6, "six": 6, "saat": 7, "seven": 7,
    "aath": 8, "eight": 8, "nau": 9, "nine": 9, "das": 10, "ten": 10, "gyarah": 11, "eleven": 11,
    "barah": 12, "twelve": 12, "pandrah": 15, "fifteen": 15, "bees": 20, "twenty": 20,
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _digits_for_number_words(t: str) -> str:
    """"do din" / "ten days" / "two weeks" -> "2 din" / "10 days" / "2 weeks".
    Only when a unit follows, so an ordinary "do" in a sentence is untouched."""
    pat = r"\b(" + "|".join(_NUM_WORDS) + r")\s+(din|dino|days?|hafte|hafta|haftey|weeks?)\b"
    return re.sub(pat, lambda m: f"{_NUM_WORDS[m.group(1)]} {m.group(2)}", t)


def _next_occurrence(today: date, dom: int, month: int | None = None) -> date | None:
    for add_month in (0, 1):
        month_idx = (month - 1 if month else today.month - 1) + add_month
        y, mth = today.year + month_idx // 12, month_idx % 12 + 1
        try:
            cand = date(y, mth, dom)
        except ValueError:
            continue
        if cand > today:
            return cand
        if month:   # a named month: next year, not next month
            try:
                cand = date(today.year + 1, month, dom)
            except ValueError:
                return None
            return cand if cand > today else None
    return None


def parse_promise_date(text: str, today: date) -> date | None:
    """Deterministic Hinglish/English date reader (also used to sanity-check
    an LLM-extracted date). Returns None when no date is stated."""
    t = _digits_for_number_words(_romanize_hints(text).lower())
    if "parso" in t or "day after tomorrow" in t:
        return today + timedelta(days=2)
    m = re.search(r"(\d{1,2})\s*(?:tareekh|tarikh|date)", t) \
        or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t)
    if m:
        dom = int(m.group(1))
        if 1 <= dom <= 31:
            d = _next_occurrence(today, dom)
            if d is not None:
                return d
    m = re.search(r"\b(\d{1,2})\s*(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", t)
    if m and 1 <= int(m.group(1)) <= 31:
        d = _next_occurrence(today, int(m.group(1)), _MONTHS[m.group(2)])
        if d is not None:
            return d
    # "in 10 days", "after 10 days", "10 days later", "10 din baad / mein"
    m = re.search(r"(\d{1,3})\s*(?:din|dino|days?)\b", t)
    if m:
        return today + timedelta(days=int(m.group(1)))
    m = re.search(r"(\d{1,2})\s*(?:weeks?|hafte|hafta|haftey)\b", t)
    if m:
        return today + timedelta(days=7 * int(m.group(1)))
    if re.search(r"\b(?:in|after|within)\s+a\s+week\b|\ba\s+week\s+(?:later|from now)\b", t):
        return today + timedelta(days=7)
    if re.search(r"\bkal\b(?!\s*(?:call|phone))", t) or "tomorrow" in t:
        return today + timedelta(days=1)
    if "agle hafte" in t or "next week" in t:
        return today + timedelta(days=7)
    return None


def keyword_classify(transcript: str, today: date) -> dict:
    t = _romanize_hints(transcript).lower()
    if not t.strip():
        return {"intent": "UNCLEAR", "promise_date": None}
    short = short_replies.read(transcript)
    if short == short_replies.NO:
        return {"intent": "REFUSED", "promise_date": None}
    if short in (short_replies.LATER, short_replies.HOLD):
        return {"intent": "CALLBACK", "promise_date": None}
    if "galat number" in t or "wrong number" in t:
        return {"intent": "WRONG_NUMBER", "promise_date": None}
    if "mat karna" in t or any(n in t for n in ("nahi karenge", "nahi karunga", "nahi dunga", "nahi denge",
                                                   "nahin karunga", "nahi kar paunga", "won't pay", "will not pay",
                                                   "not going to pay")) \
            and "bill galat" not in t and "damaged" not in t:
        return {"intent": "REFUSED", "promise_date": None}
    if any(k in t for k in ("bill galat", "galat hai", "damaged", "dispute", "already paid",
                              "pay kar diya", "pehle hi",
                              # "I did not authorise this debit" - routed on as an unauthorised-debit claim
                              "authorise nahi", "authorize nahi", "authorised nahi", "not authorise", "did not authorise",
                              "didn't authorise", "unauthori", "bina permission", "bina puchhe", "without my permission",
                              "bina mere", "mere permission ke bina")):
        return {"intent": "DISPUTE", "promise_date": None}
    if any(k in t for k in ("busy", "thodi der baad", "baad mein call", "kal call", "call back")):
        return {"intent": "CALLBACK", "promise_date": None}
    d = parse_promise_date(t, today)
    # "send me the link / I am paying now" is a request to pay NOW, even though it also
    # contains "will pay"; it only counts as a promise when a date was actually given.
    pay_now = (any(k in t for k in ("abhi bhej", "bhej deta", "bhej raha", "link mil", "pay kar raha",
                                    "send me the link", "send the link", "payment link now", "link bhej",
                                    "bhej do", "bhej dijiye", "payment kar deta", "pay kar deta", "kar deta hoon"))
               or ("abhi" in t and ("bhej" in t or "link" in t)))
    if pay_now and d is None:
        return {"intent": "PAY_NOW", "promise_date": None}
    if d is not None or any(k in t for k in ("kar dunga", "karunga", "kar denge", "pakka",
                                              "will pay", "i'll pay", "i will pay")):
        return {"intent": "PROMISE", "promise_date": d}
    if pay_now:
        return {"intent": "PAY_NOW", "promise_date": None}
    return {"intent": "UNCLEAR", "promise_date": None}


_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "record_call_outcome",
        "description": "Record what the customer meant on a payment-recovery call.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": INTENTS,
                           "description": "The customer's intent, from this fixed list only."},
                "promise_date": {"type": ["string", "null"],
                                 "description": "ISO date (YYYY-MM-DD) ONLY if the customer stated "
                                                "a specific payment date or relative day; else null."},
            },
            "required": ["intent", "promise_date"],
        },
    },
}


def _tool_for(intents: list[str]) -> dict:
    tool = json.loads(json.dumps(_CLASSIFY_TOOL))
    tool["function"]["parameters"]["properties"]["intent"]["enum"] = intents
    return tool


_QUESTION_HELP = (
    " The customer may also ASK something instead of deciding. Question intents: "
    "Q_AMOUNT = how much do I owe ('kitna hai', 'how much'); "
    "Q_WHY = what is this for / why was I charged or called ('yeh kis liye hai', 'kyun', 'what is this'); "
    "Q_DUE = when is it due; "
    "Q_HOW_PAY = HOW would I pay, they are asking for information ('kaise karun', 'how do I pay'); "
    "Q_WHO = who are you / which company is calling ('aap kaun', 'who is this'); "
    "Q_PARTIAL = can I pay part of it or in instalments; "
    "Q_REPEAT = say that again; "
    "Q_OTHER = wants a person or manager, a complaint, a legal threat, or anything else outside these. "
    "IMPORTANT: if they ASK FOR or AGREE TO RECEIVE the payment link ('send me the link', 'link bhej "
    "do', 'bhej dijiye'), or say they are paying right now, that is the DECISION PAY_NOW, not a "
    "question. If they both ask and decide, choose the decision."
)


def _llm_classify(transcript: str, today: date, intents: list[str] | None = None) -> dict:
    intents = intents or INTENTS
    if runtime_flags.is_llm_failure_forced():
        raise RuntimeError("LLM failure forced via /debug/toggle-llm-failure")
    if not settings.groq_api_key:
        raise RuntimeError("No GROQ_API_KEY configured")
    prompt = (
        "A customer was called about an unpaid debt. The transcript is Hinglish (Hindi in Roman "
        "script mixed with English). Classify their intent. 'kal' means tomorrow, 'parso' the day "
        f"after tomorrow. Today is {today.isoformat()}."
        + (_QUESTION_HELP if len(intents) > len(INTENTS) else "")
        + f"\n\nTranscript:\n{transcript}"
    )
    # Groq answers 400 "tool_use_failed" when the model's own tool-call JSON
    # comes out malformed (seen ~1 in 6 with gpt-oss). It is a one-off
    # generation slip, not a bad request, so retry once before falling back.
    for attempt in (1, 2):
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
            json={"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
                  "tools": [_tool_for(intents)],
                  "tool_choice": {"type": "function", "function": {"name": "record_call_outcome"}},
                  "max_tokens": 200},
            timeout=8.0,
        )
        if resp.status_code == 400 and attempt == 1 and "tool_use_failed" in resp.text:
            continue
        break
    resp.raise_for_status()
    args = json.loads(resp.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    if args.get("intent") not in intents:
        raise RuntimeError(f"out-of-schema intent {args.get('intent')}")
    pd = None
    if args.get("promise_date"):
        try:
            pd = date.fromisoformat(args["promise_date"])
        except ValueError:
            pd = None
    return {"intent": args["intent"], "promise_date": pd}


def classify_call_outcome(db: Session, transcript: str, today: date) -> dict:
    """LLM first, keyword classifier on any failure - never a silent gap."""
    try:
        out = _llm_classify(transcript, today)
        out["source"] = "llm"
        return out
    except Exception as e:   # network, parse, no key, forced failure: all fall back
        write_audit_entry(db, "llm_failure_fallback", {"error": str(e), "stage": "voice_intent"})
        out = keyword_classify(transcript, today)
        out["source"] = "keyword_fallback"
        return out


# ── Placing a call ───────────────────────────────────────────────────────────

def _next_window_day_10am(now: datetime) -> datetime:
    d = (now + _IST).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, 10, 0) - _IST


@dataclass
class CallTarget:
    subject_type: str
    subject_id: str
    contact: ContactPoint | None
    name: str
    amount_inr: float
    script_kind: str
    due: str = ""
    ref: str = ""
    blocked: str | None = None   # subject-level reason it must not be called now


def _target_invoice(db: Session, invoice_id: str, now: datetime) -> CallTarget | None:
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv is None:
        return None
    acct = db.query(BusinessAccount).filter(BusinessAccount.id == inv.account_id).first()
    cp = contacts.for_business_account(db, inv.account_id)
    t = CallTarget("invoice", inv.id, cp, (acct.contact_name or acct.company_name) if acct else "Sir/Madam",
                   receivables._outstanding(inv), "invoice_overdue",
                   due=(inv.due_at + _IST).strftime("%d %b"), ref=inv.invoice_no)
    if inv.settled:
        t.blocked = "invoice_closed"
    elif inv.status == InvoiceStatus.DISPUTED.value:
        t.blocked = "invoice_disputed"
    elif db.query(PaymentPromise).filter(
            PaymentPromise.subject_type == "invoice", PaymentPromise.subject_id == inv.id,
            PaymentPromise.status == PromiseStatus.OPEN.value).first():
        t.blocked = "promise_open"
    return t


def _target_mandate(db: Session, run_id: str, now: datetime) -> CallTarget | None:
    run = db.query(MandateRun).filter(MandateRun.id == run_id).first()
    if run is None:
        return None
    cp = contacts.for_razorpay_customer(db, run.razorpay_customer_id)
    t = CallTarget("mandate_run", run.id, cp, "Customer", run.amount_inr, "mandate_failed",
                   ref=run.subscription_id)
    if run.risk_settled or run.status != MandateStatus.ACTIVE.value:
        t.blocked = "run_not_active"
    elif run.next_action_at is not None and run.next_action_at <= now:
        # the retry already fired - a "fund your account" call is moot
        t.blocked = "retry_already_due"
    elif db.query(PaymentPromise).filter(
            PaymentPromise.subject_type == "mandate_run", PaymentPromise.subject_id == run.id,
            PaymentPromise.status == PromiseStatus.OPEN.value).first():
        t.blocked = "promise_open"
    return t


def _target_order(db: Session, order_id: str, now: datetime) -> CallTarget | None:
    order = db.query(Order).filter(Order.id == order_id).first()
    if order is None:
        return None
    customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
    cp = contacts.for_customer(db, order.customer_id)
    t = CallTarget("order", order.id, cp, customer.name if customer else "Customer",
                   order.amount_inr, "order_failed", ref=order.razorpay_order_id or "")
    # Open run == the carrier order is still FAILED with its at-risk unsettled
    # (same predicate webhook._open_run_carrier uses).
    if order.status != OrderStatus.FAILED or order.risk_settled:
        t.blocked = "payment_resolved"
    elif order.amount_inr < runtime_flags.get_voice_min_order_value():
        t.blocked = "below_min_order_value"
    return t


_ADAPTERS = {"invoice": _target_invoice, "mandate_run": _target_mandate, "order": _target_order}


def _guard(db: Session, target: CallTarget, now: datetime) -> str | None:
    """Returns a block reason, or None when the call may go ahead."""
    if target.blocked:
        return target.blocked
    ok, reason = contacts.can_call(target.contact)
    if not ok:
        return reason
    ready, reason = _provider_ready()
    if not ready:
        return reason
    # DND is separate from consent: a customer can have ticked "call me" and
    # still be registered. Fails closed (see app/dnd.py).
    ok, reason = dnd.check(target.contact.phone)
    if not ok:
        return reason
    if not in_contact_window(now):
        return "outside_contact_window"
    calls = db.query(VoiceCall).filter(
        VoiceCall.subject_type == target.subject_type, VoiceCall.subject_id == target.subject_id).all()
    if len(calls) >= runtime_flags.get_voice_max_attempts_per_subject():
        return "max_attempts_reached"
    if any(c.created_at and c.created_at > now - timedelta(hours=24) for c in calls):
        return "one_call_per_day"
    return None


def place_call(db: Session, subject_type: str, subject_id: str,
               now: datetime | None = None) -> tuple[bool, str]:
    """Place (and route the outcome of) one recovery call. Returns
    (placed, outcome_label); a refused call is (False, "voice_blocked:<why>")."""
    now = now or clock.now()
    adapter = _ADAPTERS.get(subject_type)
    target = adapter(db, subject_id, now) if adapter else None
    if target is None:
        return False, "voice_blocked:unknown_subject"

    block = _guard(db, target, now)
    if block:
        write_audit_entry(db, "voice_call_blocked", {
            "subject_type": subject_type, "subject_id": subject_id, "reason": block})
        if block == "max_attempts_reached":
            execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
            return True, "voice_blocked:max_attempts_reached:escalated"
        return False, f"voice_blocked:{block}"

    cp = target.contact
    script = build_script(target.script_kind, cp.preferred_language or contacts.DEFAULT_LANGUAGE, target.name,
                          target.amount_inr, due=target.due, ref=target.ref)
    result = _provider.place_call(cp.phone, script, cp.preferred_language or contacts.DEFAULT_LANGUAGE)
    call = VoiceCall(
        subject_type=subject_type, subject_id=subject_id, customer_ref=cp.id, phone=cp.phone,
        language=cp.preferred_language or contacts.DEFAULT_LANGUAGE, script_text=script, status=result.status,
        transcript=result.transcript, duration_s=result.duration_s,
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    write_audit_entry(db, "voice_call_placed", {
        "call_id": call.id, "subject_type": subject_type, "subject_id": subject_id,
        "language": call.language, "status": result.status})
    routed = _route(db, call, target, now)
    return True, f"voice_call_placed:{routed}"


def start_interactive_call(db: Session, subject_type: str, subject_id: str,
                           now: datetime | None = None) -> dict:
    """Ring a call whose "customer" is a person at a browser (Demo Console).

    Same adapter, same guardrails (verified number, customer-ticked consent,
    DND, window, daily/attempt caps), same script - only the provider differs:
    nothing is dialled and the reply is typed or spoken into the page. The
    call stays `in_progress` until finish_interactive_call() routes it."""
    now = now or clock.now()
    adapter = _ADAPTERS.get(subject_type)
    target = adapter(db, subject_id, now) if adapter else None
    if target is None:
        return {"ok": False, "block": "unknown_subject"}
    block = _guard(db, target, now)
    if block:
        write_audit_entry(db, "voice_call_blocked", {
            "subject_type": subject_type, "subject_id": subject_id, "reason": block})
        return {"ok": False, "block": block}
    cp = target.contact
    lang = cp.preferred_language or contacts.DEFAULT_LANGUAGE
    script = build_script(target.script_kind, lang, target.name, target.amount_inr,
                          due=target.due, ref=target.ref)
    call = VoiceCall(subject_type=subject_type, subject_id=subject_id, customer_ref=cp.id,
                     phone=cp.phone, language=lang, script_text=script, status="in_progress",
                     transcript="")
    db.add(call)
    db.commit()
    db.refresh(call)
    write_audit_entry(db, "voice_call_placed", {
        "call_id": call.id, "subject_type": subject_type, "subject_id": subject_id,
        "language": lang, "status": "in_progress", "interactive": True})
    return {"ok": True, "call_id": call.id, "script": script, "language": lang,
            "phone_masked": contacts.mask_phone(cp.phone)}


def finish_interactive_call(db: Session, call_id: str, transcript: str,
                            now: datetime | None = None, force: tuple | None = None) -> dict:
    """Route what the person said. An empty transcript is "no answer"."""
    now = now or clock.now()
    text = (transcript or "").strip()
    status = "completed" if text else "no_answer"
    claimed = (
        db.query(VoiceCall)
        .filter(VoiceCall.id == call_id, VoiceCall.status == "in_progress")
        .update({"status": status, "transcript": text, "duration_s": 12 + len(text) // 8})
    )
    db.commit()
    if not claimed:
        return {"ok": False, "error": "call_not_in_progress"}
    call = db.query(VoiceCall).filter(VoiceCall.id == call_id).first()
    adapter = _ADAPTERS.get(call.subject_type)
    target = adapter(db, call.subject_id, now) if adapter else None
    if target is None:
        return {"ok": False, "error": "subject_missing"}
    routed = _route(db, call, target, now, force)
    try:                         # notes for future calls (only happens with the customer's memory consent)
        from app import memory
        memory.schedule_after_call(call_id)
    except Exception:
        logger.exception("could not schedule memory notes")
    return {"ok": True, "status": status, "intent": call.intent, "intent_source": call.intent_source,
            "promise_date": call.extracted_promise_date.isoformat() if call.extracted_promise_date else None,
            "routed_to": routed}


def settle_promise_date(intent: str, source: str, pdate: date | None, transcript: str,
                        today: date) -> date | None:
    """An LLM-extracted date is only trusted if the deterministic reader agrees
    there IS a date in the words; otherwise it is discarded and the reader's own
    date (if any) is used. Shared by the single-shot and the multi-turn call."""
    if intent == "PROMISE" and pdate is not None and source == "llm" \
            and parse_promise_date(transcript, today) is None:
        pdate = None
    if intent == "PROMISE" and pdate is None:
        pdate = parse_promise_date(transcript, today)
    return pdate


def _understand(db: Session, call: VoiceCall, now: datetime) -> tuple[str, str, date | None]:
    today = (now + _IST).date()
    if call.status != "completed" or not (call.transcript or "").strip():
        return "CALLBACK", "no_answer", None   # no answer == try again later
    c = classify_call_outcome(db, call.transcript, today)
    intent, source, pdate = c["intent"], c["source"], c.get("promise_date")
    return intent, source, settle_promise_date(intent, source, pdate, call.transcript, today)


def _stop_voice(db: Session, cp: ContactPoint) -> None:
    cp.wrong_number_at = clock.now()
    cp.voice_consent = False
    db.commit()
    write_audit_entry(db, "voice_contact_flagged_wrong_number", {
        "contact_id": cp.id, "phone": contacts.mask_phone(cp.phone)})


def _callback(db: Session, target: CallTarget, now: datetime) -> str:
    schedule(db, ScheduledKind.VOICE_CALL, target.subject_type, target.subject_id,
             _next_window_day_10am(now), payload={"ref": target.ref},
             customer_ref=target.contact.id if target.contact else None)
    return "callback_scheduled"


def _send_link(db: Session, subject_type: str, subject_id: str) -> str:
    """The link promised on the call, through the messaging layer. Returns the
    routed label - "payment_link_sent" only when a channel actually took it."""
    msg = None
    if subject_type == "invoice":
        inv = db.query(Invoice).filter(Invoice.id == subject_id).first()
        if inv is not None:
            msg = notify.invoice_message(db, inv, "invoice_overdue")
    elif subject_type == "mandate_run":
        run = db.query(MandateRun).filter(MandateRun.id == subject_id).first()
        if run is not None:
            msg = notify.mandate_message(db, run, "payment_link")
    elif subject_type == "order":
        order = db.query(Order).filter(Order.id == subject_id).first()
        if order is not None:
            msg = notify.order_payment_link(db, order)
    if msg is None:
        return "payment_link_not_sent:no_subject"
    return "payment_link_sent" if messaging.delivered(msg) else f"payment_link_not_sent:{msg.status}"


def _route(db: Session, call: VoiceCall, target: CallTarget, now: datetime,
           force: tuple | None = None) -> str:
    # `force` = (intent, source, date): the multi-turn call already knows this
    # goes to a person (an out-of-scope question), so it is not re-classified.
    intent, source, pdate = force or _understand(db, call, now)
    call.intent, call.intent_source, call.extracted_promise_date = intent, source, pdate
    cp = target.contact
    st, sid = target.subject_type, target.subject_id
    routed = intent.lower()

    if intent == "PAY_NOW":
        routed = _send_link(db, st, sid)

    elif intent == "PROMISE":
        if st == "order":
            # Promises on failed storefront orders are deliberately not
            # tracked (a shopper owes nothing - see app/order_links.py). The
            # link goes now, and the day they named gets ONE reminder.
            routed = _send_link(db, st, sid)
            order = db.query(Order).filter(Order.id == sid).first()
            if pdate is not None and order is not None:
                from app.order_links import schedule_reminder
                if schedule_reminder(db, order, pdate) is not None:
                    routed += f"_reminder_{pdate.isoformat()}"
        else:
            promise, code, _ = (None, "no_date", "")
            if pdate is not None:
                promise, code, _ = promises.create_promise(db, st, sid, pdate, "voice", now)
            if promise is None:
                execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
                routed = f"escalated:promise_rejected:{code}"
            else:
                routed = f"promise_created:{pdate.isoformat()}"

    elif intent == "DISPUTE":
        if st == "invoice":
            inv = db.query(Invoice).filter(Invoice.id == sid).first()
            if inv is not None:
                receivables.dispute(db, inv, "raised on recovery call")
            routed = "invoice_disputed"
        elif st == "mandate_run":
            # "I didn't authorise this" is an unauthorised-debit claim, not a
            # billing quarrel: stop every automatic attempt on the mandate NOW
            # and put it in front of a person.
            cancel_for_subject(db, st, sid, reason="unauthorised_debit_claim")
            run = db.query(MandateRun).filter(MandateRun.id == sid).first()
            if run is not None and not run.risk_settled:
                run.status = MandateStatus.EXHAUSTED.value
                db.commit()
            execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
            write_audit_entry(db, "mandate_unauthorised_debit_claim", {
                "mandate_run_id": sid, "call_id": call.id})
            routed = "escalated:unauthorised_debit_claim"
        else:
            execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
            routed = "escalated:payment_dispute"

    elif intent == "WRONG_NUMBER":
        if cp is not None:
            _stop_voice(db, cp)
        routed = "voice_stopped_wrong_number"

    elif intent == "CALLBACK":
        routed = _callback(db, target, now)

    else:   # REFUSED / UNCLEAR - a person should look at it
        execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
        routed = f"escalated:{intent.lower()}"

    call.routed_to = routed
    db.commit()
    write_audit_entry(db, "voice_call_outcome", {
        "call_id": call.id, "subject_type": st, "intent": intent, "intent_source": source,
        "promise_date": pdate.isoformat() if pdate else None, "routed_to": routed})
    return routed


# ── Wiring into the receivables ladder + scheduler ───────────────────────────

# Demo Console switch: when on, calls the SCHEDULER places do not use the
# canned simulated customer - they are left ringing (`in_progress`) for a
# person at the browser to answer (start/finish_interactive_call). In-memory
# and refused in production, like the demo clock.
_interactive = {"on": False}


def set_interactive_mode(on: bool) -> None:
    if on and settings.environment == "production":
        raise RuntimeError("Interactive demo calls are disabled in production.")
    _interactive["on"] = bool(on)


def interactive_mode() -> bool:
    return _interactive["on"]


def _dial(db: Session, subject_type: str, subject_id: str) -> tuple[bool, str]:
    if not _interactive["on"]:
        return place_call(db, subject_type, subject_id)
    r = start_interactive_call(db, subject_type, subject_id)
    if r["ok"]:
        return True, "voice_call_ringing"
    if r["block"] == "max_attempts_reached":
        execute_action(AgentAction.ESCALATE_TO_HUMAN.value, None)
        return True, "voice_blocked:max_attempts_reached:escalated"
    return False, f"voice_blocked:{r['block']}"


def _dispatch_invoice(db: Session, inv: Invoice, account: BusinessAccount) -> tuple[bool, str]:
    return _dial(db, "invoice", inv.id)


receivables.register_voice_dispatcher(_dispatch_invoice)


def _handle_scheduled_call(db: Session, action: ScheduledAction, payload: dict) -> tuple[str, str]:
    """A scheduled call: a "call me later" callback, the call before a
    mandate retry, or the follow-up call on a failed order. Eligibility is
    re-judged NOW - consent, the window and the debt's state may all have
    changed since it was scheduled."""
    placed, outcome = _dial(db, action.subject_type, action.subject_id)
    if not placed:
        return ScheduledStatus.SKIPPED.value, outcome
    return ScheduledStatus.DONE.value, outcome


register_handler(ScheduledKind.VOICE_CALL, _handle_scheduled_call)


# ── API ──────────────────────────────────────────────────────────────────────

class ScenarioIn(BaseModel):
    scenario: str
    promise_days: int | None = None


class TriggerIn(BaseModel):
    subject_type: str = "invoice"
    subject_id: str | None = None
    invoice_id: str | None = None      # back-compat with the first version


@router.post("/debug/voice-scenario")
def voice_scenario(req: ScenarioIn):
    """Pick what the simulated customer says on the next call(s)."""
    try:
        set_simulated_scenario(req.scenario, req.promise_days)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"scenario": _sim["scenario"], "promise_days": _sim["promise_days"],
            "options": sorted(_SCENARIOS)}


@router.post("/voice/calls/trigger")
def trigger_call(req: TriggerIn, db: Session = Depends(get_db)):
    """Merchant-initiated call. Still goes through every guardrail (verified
    number, customer-ticked consent, window, attempts) - a button cannot
    bypass them."""
    subject_id = req.subject_id or req.invoice_id
    if req.subject_type not in _ADAPTERS or not subject_id:
        raise HTTPException(422, f"subject_type must be one of {sorted(_ADAPTERS)} with a subject_id")
    placed, outcome = place_call(db, req.subject_type, subject_id)
    return {"placed": placed, "outcome": outcome}


@router.get("/voice/calls")
def list_calls(db: Session = Depends(get_db)):
    rows = db.query(VoiceCall).order_by(VoiceCall.created_at.desc()).limit(200).all()
    return [{
        "id": c.id, "subject_type": c.subject_type, "subject_id": c.subject_id,
        "language": c.language, "status": c.status, "script_text": c.script_text,
        "transcript": c.transcript, "intent": c.intent, "intent_source": c.intent_source,
        "extracted_promise_date": c.extracted_promise_date.isoformat() if c.extracted_promise_date else None,
        "routed_to": c.routed_to, "created_at": c.created_at,
    } for c in rows]

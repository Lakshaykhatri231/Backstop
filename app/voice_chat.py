"""
Multi-turn interactive calls (Demo Console).

A call used to be one exchange. This lets the customer ask things first -
"how much is it?", "why was I charged?", "can I pay half?" - and still end in
one of the same outcomes (pay now / promise / dispute / ...).

How it stays bounded:
- The opening line and the closing lines are fixed text. In between, the model
  may WRITE the reply (so it sounds like a person answering, not a script) -
  but only from the debt's own facts and a set of approved answers it is
  handed, and CODE then checks the sentence before it is spoken: no number
  that is not the real amount / invoice / date (or something the customer just
  said), no fee / interest / discount / waiver / legal / threat wording, no
  claim to be a person, no links, right script, short. Any failure falls back
  to the fixed answer template, and the rejection is audited. The model never
  decides an action: intent is still routed by voice.finish_interactive_call.
- Small human touches (a short "acha," / "hmm,", a kind line when the customer
  mentions hardship) are chosen by code from fixed lists, at most one per
  reply, so they cannot drift or pile up.
- Questions it cannot answer from those facts (complaints, legal threats,
  "give me your manager", anything unlisted) hand the case to a person - they
  are not improvised.
- Hard limits: MAX_TURNS customer lines per call, one clarifying re-ask for an
  unclear reply, then a person.
- The call still ends through voice.finish_interactive_call, so the real
  decision (promise dates via validate_promise, link sending, dispute, wrong
  number, escalation) is exactly the single-shot code path.
- Honest about what it is: the opening line says the call is automated and may
  be recorded, and "who are you" repeats that.
"""
import json
import re
import time
from datetime import date

import httpx
from sqlalchemy.orm import Session

from app import clock, contacts, customer_profile, memory, memory_retrieval, persona, retrieval, runtime_flags, short_replies, voice
from app.audit import write_audit_entry
from app.config import settings
from app.messaging import MERCHANT_NAME
from app.models import VoiceCall, VoiceTurn

MAX_TURNS = 6

QUESTION_INTENTS = ["Q_AMOUNT", "Q_WHY", "Q_DUE", "Q_HOW_PAY", "Q_WHO", "Q_PARTIAL", "Q_REPEAT", "Q_POLICY", "Q_MEMORY",
                    "Q_OTHER"]
# Answered from the debt's own facts. Q_POLICY (merchant documents), Q_MEMORY (what was said on an earlier call) and
# Q_OTHER (hand-off) have their own paths.
_ANSWERABLE = [q for q in QUESTION_INTENTS if q not in ("Q_OTHER", "Q_POLICY", "Q_MEMORY")]


# ── Understanding a turn ─────────────────────────────────────────────────────

_DEVA_Q = (
    ("कितना", "kitna"), ("कितने", "kitna"), ("क्यों", "kyun"), ("कौन", "kaun"), ("कैसे", "kaise"),
    ("आधा", "aadha"), ("किस्त", "kist"), ("किश्त", "kist"), ("मैनेजर", "manager"),
    ("शिकायत", "complaint"), ("वकील", "vakeel"), ("दोबारा बोल", "dobara bol"),
    ("फिर से बोल", "phir se bol"), ("दोहराइए", "repeat"), ("समझ नहीं", "samajh nahi"),
    ("इंसान", "insaan"), ("किसलिए", "kis liye"), ("किस लिए", "kis liye"),
)

# Most specific first. Matched against the lower-cased, Devanagari-romanised text.
_Q_RULES = (
    ("Q_OTHER", ("manager", "vakeel", "lawyer", "legal", "court",
                 "complaint", "police", "harass", "consumer forum", "rbi")),
    ("Q_REPEAT", ("repeat", "dobara bol", "phir se bol", "samajh nahi", "sunai nahi", "say again",
                  "pardon", "kya kaha")),
    ("Q_WHO", ("kaun", "who are you", "who is this", "who's this", "kahan se bol", "kis company")),
    ("Q_PARTIAL", ("partial", "half", "aadha", "kist", "installment", "instalment", "thoda thoda",
                   "kuch hissa", "part payment", "in parts")),
    ("Q_POLICY", ("late fee", "fee", "charges", "charge lagega", "penalty", "interest", "refund", "wapas milega",
                  "paisa wapas", "cancel", "mandate", "bank account change", "account change", "update my",
                  "policy", "plan", "subscription", "support hours", "support timing", "payment method",
                  "kaun se payment", "upi", "card se", "netbanking", "net banking", "dispute kaise", "kitne din mein")),
    ("Q_AMOUNT", ("kitna", "how much", "kitne paise", "kitne rupe", "amount kya")),
    ("Q_DUE", ("due date", "kab due", "when is it due", "deadline", "last date", "kab tak karna",
                "kab tak dena", "kab dena", "kab tak pay", "by when")),
    ("Q_HOW_PAY", ("kaise", "how do i pay", "how to pay", "kahan pay", "kis tarah", "link kahan")),
    ("Q_WHY", ("kyun", "kyon", "why", "kis liye", "kis baat", "kis cheez", "what is this", "kya hai yeh")),
)


# "Are you a robot / a real person?" is a question about the caller (answered
# honestly: an AI assistant), unlike "let me talk to a person", which is a
# request to hand over. Checked first, as patterns.
_ASKS_IF_AI = re.compile(
    r"(are you|kya aap|aap kya|aap).{0,14}(human|robot|\bbot\b|machine|\bai\b|insaan|real|asli|computer|recorded)")
_WANTS_PERSON = re.compile(
    r"(talk|speak|baat|connect|transfer|give).{0,18}(human|person|insaan|manager|someone|kisi)"
    r"|(human|person|insaan|manager|kisi).{0,16}(se baat|se milao|se connect|talk|speak)")


def keyword_question(text: str) -> str | None:
    t = voice._romanize_hints(text or "")
    for src, dst in _DEVA_Q:
        t = t.replace(src, f" {dst} ")
    t = t.lower()
    if _WANTS_PERSON.search(t):
        return "Q_OTHER"
    if _ASKS_IF_AI.search(t):
        return "Q_WHO"
    if memory_retrieval.has_recall_cue(t):      # "pichli baar maine kya kaha tha" - about the earlier call
        return "Q_MEMORY"                       # (before the word rules: its "kya kaha" is not "say that again")
    t = re.sub(r"\bkaun(si|sa|se)\b", "konsi", t)    # "which date" is not "who are you"
    for intent, keys in _Q_RULES:
        if any(k in t for k in keys):
            return intent
    return None


def classify_turn(db: Session, transcript: str, today: date) -> dict:
    """LLM first (decision OR question), keyword reader on any failure."""
    try:
        out = voice._llm_classify(transcript, today, voice.INTENTS + QUESTION_INTENTS)
        out["source"] = "llm"
        return out
    except Exception as e:
        write_audit_entry(db, "llm_failure_fallback", {"error": str(e), "stage": "voice_turn"})
        out = voice.keyword_classify(transcript, today)
        out["source"] = "keyword_fallback"
        if out["intent"] == "UNCLEAR":
            q = keyword_question(transcript)
            if q:
                out["intent"] = q
        return out


# ── What the agent says (all fixed text) ─────────────────────────────────────

_ANSWERS = {
    # Fixed lines, written for a voice that reads punctuation as rhythm: short sentences of
    # varied length, "..." only for a real pause, "!" only for genuine warmth. The facts and
    # the guardrails (no fees / waivers / legal wording, honest about being an AI) are unchanged.
    "hinglish": {
        "Q_AMOUNT": "Bakaya amount {amount} hai.",
        "Q_WHY": {
            "invoice_overdue": "Invoice {ref} ka payment {due} tak {merchant} ko mila nahi. Bas isiliye call kiya hai.",
            "mandate_failed": "Aapke account se {amount} ka monthly autopay debit complete nahi ho paya. Isiliye call kiya hai.",
            "order_failed": "Aapka {amount} ka payment complete nahi ho paya tha. Bas isiliye call kiya hai.",
        },
        "Q_DUE": {
            "invoice_overdue": "Invoice {ref} ki due date {due} thi.",
            "mandate_failed": "Yeh debit abhi pending hai. Hum dobara try karenge, aur usse pehle aapko notice milega.",
            "order_failed": "Yeh payment abhi pending hai. Link se aap ise abhi complete kar sakte hain.",
        },
        "Q_HOW_PAY": {
            "invoice_overdue": "Payment ek secure link se hota hai. Aap bas bol dijiye, main link abhi bhej deta hoon.",
            "order_failed": "Payment ek secure link se hota hai. Aap bas bol dijiye, main link abhi bhej deta hoon.",
            "mandate_failed": "Aapko bas apne bank account mein {amount} rakhna hai. Hum dobara debit try karenge, aur usse pehle aapko notice milega.",
        },
        "Q_WHO": "Main {merchant} ka AI assistant hoon... koi insaan nahi. Aur yeh call record ho sakti hai. Aap chahein to main kisi insaan se connect karwa sakta hoon.",
        "Q_PARTIAL": {
            "invoice_overdue": "Haan, link se aap kuch hissa bhi de sakte hain.",
            "mandate_failed": "Autopay debit poora {amount} ek saath hota hai. Is call par amount badal nahi sakta.",
            "order_failed": "Is call par amount badal nahi sakta. Link par poora {amount} dena hoga.",
        },
        "memory_lead": "{when} ki hamari call ka mera note yeh hai:",
        "no_memory": "Maaf kijiye, mere paas us baat ka koi note nahi hai.",
        "hardship": "Arre, tension mat lijiye. Aap bas bata dijiye ki kab tak arrange ho payega... ya main kisi insaan se baat karwa sakta hoon.",
        "confirm_date": "Toh main {date} note kar loon? Yeh theek rahega?",
        "ask_date": "Theek hai. Kab tak payment ho payega? Aap \"teen din mein\" jaisa kuch ya koi tareekh bata sakte hain.",
        "ask_date_again": "Maaf kijiye, mujhe tareekh abhi bhi saaf nahi mili. Kya aap ek baar phir bata sakte hain, jaise \"teen din mein\" ya \"25 tareekh\"?",
        "clarify": "Maaf kijiye, mujhe thik se samajh nahi aaya. Kya aap ek baar phir bata sakte hain?",
        "hold": "Ji, koi jaldi nahi hai. Aap araam se dekh lijiye, main yahin hoon.",
        "handoff": "Theek hai, main ise ek insaan ko de deta hoon. Woh aapse sampark karenge. Dhanyavaad.",
        "closing": {
            "promise": "Bahut shukriya! Maine {date} tak ka note kar liya hai.",
            "pay_now": "Theek hai, main link abhi bhej deta hoon. Dhanyavaad!",
            "dispute": "Maine aapki baat note kar li hai. Ek insaan aapse sampark karega. Dhanyavaad.",
            "callback": "Theek hai, hum aapko baad mein call karenge. Dhanyavaad.",
            "wrong_number": "Galti ke liye maaf kijiye. Hum is number par dobara call nahi karenge.",
        },
    },
    "hi": {
        "Q_AMOUNT": "बकाया राशि {amount} है।",
        "Q_WHY": {
            "invoice_overdue": "इनवॉइस {ref} का भुगतान {due} तक {merchant} को मिला नहीं। बस इसीलिए कॉल किया है।",
            "mandate_failed": "आपके खाते से {amount} का मासिक ऑटोपे डेबिट पूरा नहीं हो पाया। इसीलिए कॉल किया है।",
            "order_failed": "आपका {amount} का भुगतान पूरा नहीं हो पाया था। बस इसीलिए कॉल किया है।",
        },
        "Q_DUE": {
            "invoice_overdue": "इनवॉइस {ref} की देय तिथि {due} थी।",
            "mandate_failed": "यह डेबिट अभी बाकी है। हम दोबारा प्रयास करेंगे, और उससे पहले आपको सूचना मिलेगी।",
            "order_failed": "यह भुगतान अभी बाकी है। लिंक से आप इसे अभी पूरा कर सकते हैं।",
        },
        "Q_HOW_PAY": {
            "invoice_overdue": "भुगतान एक सुरक्षित लिंक से होता है। आप बस कह दीजिए, मैं लिंक अभी भेज देता हूँ।",
            "order_failed": "भुगतान एक सुरक्षित लिंक से होता है। आप बस कह दीजिए, मैं लिंक अभी भेज देता हूँ।",
            "mandate_failed": "आपको बस अपने बैंक खाते में {amount} रखना है। हम दोबारा डेबिट का प्रयास करेंगे, और उससे पहले आपको सूचना मिलेगी।",
        },
        "Q_WHO": "मैं {merchant} का AI असिस्टेंट हूँ... कोई इंसान नहीं। और यह कॉल रिकॉर्ड हो सकती है। आप चाहें तो मैं किसी व्यक्ति से जुड़वा सकता हूँ।",
        "Q_PARTIAL": {
            "invoice_overdue": "हाँ, लिंक से आप कुछ हिस्सा भी दे सकते हैं।",
            "mandate_failed": "ऑटोपे डेबिट पूरा {amount} एक साथ होता है। इस कॉल पर राशि बदल नहीं सकती।",
            "order_failed": "इस कॉल पर राशि बदल नहीं सकती। लिंक पर पूरा {amount} देना होगा।",
        },
        "memory_lead": "{when} की हमारी कॉल का मेरा नोट यह है:",
        "no_memory": "माफ़ कीजिए, मेरे पास उस बात का कोई नोट नहीं है।",
        "hardship": "अरे, चिंता मत कीजिए। आप बस बता दीजिए कि कब तक इंतज़ाम हो पाएगा... या मैं किसी व्यक्ति से बात करवा सकता हूँ।",
        "confirm_date": "तो मैं {date} नोट कर लूँ? यह ठीक रहेगा?",
        "ask_date": "ठीक है। कब तक भुगतान हो पाएगा? आप \"तीन दिन में\" जैसा कुछ या कोई तारीख बता सकते हैं।",
        "ask_date_again": "माफ़ कीजिए, मुझे तारीख अभी भी साफ़ नहीं मिली। क्या आप एक बार फिर बता सकते हैं, जैसे \"तीन दिन में\" या \"25 तारीख\"?",
        "clarify": "माफ़ कीजिए, मुझे ठीक से समझ नहीं आया। क्या आप एक बार फिर बता सकते हैं?",
        "hold": "जी, कोई जल्दी नहीं है। आप आराम से देख लीजिए, मैं यहीं हूँ।",
        "handoff": "ठीक है, मैं इसे एक व्यक्ति को दे देता हूँ। वे आपसे संपर्क करेंगे। धन्यवाद।",
        "closing": {
            "promise": "बहुत शुक्रिया! मैंने {date} तक का नोट कर लिया है।",
            "pay_now": "ठीक है, मैं लिंक अभी भेज देता हूँ। धन्यवाद!",
            "dispute": "मैंने आपकी बात नोट कर ली है। एक व्यक्ति आपसे संपर्क करेगा। धन्यवाद।",
            "callback": "ठीक है, हम आपको बाद में कॉल करेंगे। धन्यवाद।",
            "wrong_number": "गलती के लिए माफ़ कीजिए। हम इस नंबर पर दोबारा कॉल नहीं करेंगे।",
        },
    },
    "en": {
        "Q_AMOUNT": "The outstanding amount is {amount}.",
        "Q_WHY": {
            "invoice_overdue": "Payment for invoice {ref} had not reached {merchant} by {due}. That is why I am calling.",
            "mandate_failed": "Your monthly autopay debit of {amount} could not be completed. That is why I am calling.",
            "order_failed": "Your payment of {amount} did not go through. That is all this call is about.",
        },
        "Q_DUE": {
            "invoice_overdue": "Invoice {ref} was due on {due}.",
            "mandate_failed": "The debit is still pending. We will retry, and you will get a notice before that.",
            "order_failed": "The payment is still pending. You can finish it right now with the link.",
        },
        "Q_HOW_PAY": {
            "invoice_overdue": "It is a secure link. Just say the word, and I will send it right away.",
            "order_failed": "It is a secure link. Just say the word, and I will send it right away.",
            "mandate_failed": "You only need to keep {amount} in your bank account. We will retry the debit, and you will get a notice before that.",
        },
        "Q_WHO": "I am an AI assistant calling for {merchant}... not a person. And this call may be recorded. If you like, I can connect you with a person.",
        "Q_PARTIAL": {
            "invoice_overdue": "Yes, you can pay a part of it through the link.",
            "mandate_failed": "The autopay debit takes the full {amount} at once. It cannot be changed on this call.",
            "order_failed": "The amount cannot be changed on this call. The link asks for the full {amount}.",
        },
        "memory_lead": "My note from our call on {when} says:",
        "no_memory": "Sorry, I do not have a note about that.",
        "hardship": "Oh, please do not worry. Just tell me by when you could arrange it... or I can put you in touch with a person.",
        "confirm_date": "So shall I note {date} for the payment? Would that work?",
        "ask_date": "Alright. By when can you pay? You can say something like \"in three days\", or give a date.",
        "ask_date_again": "Sorry, I still did not catch the date. Could you say it once more, for example \"in three days\" or \"the 25th\"?",
        "clarify": "Sorry, I did not quite catch that. Could you say it once more?",
        "hold": "Of course, no rush. Take your time, I am right here.",
        "handoff": "Understood. I will pass this to a person, and they will contact you. Thank you.",
        "closing": {
            "promise": "Thank you so much! I have noted {date}.",
            "pay_now": "Alright, I am sending the link right now. Thank you!",
            "dispute": "I have noted what you said. A person will contact you. Thank you.",
            "callback": "Alright, we will call you again later. Thank you.",
            "wrong_number": "Sorry for the mistake. We will not call this number again.",
        },
    },
}


def _lang(language: str) -> dict:
    return _ANSWERS.get(language) or _ANSWERS["hinglish"]


def _say(language: str, key: str) -> str:
    """A fixed one-liner (clarify / handoff / hardship) in the persona's verb gender."""
    return persona.apply_gender(_lang(language)[key])


def _facts(target) -> dict:
    return {"amount": voice._inr(target.amount_inr), "due": target.due, "ref": target.ref,
            "merchant": MERCHANT_NAME}


def _pick(block, kind: str):
    return block[kind] if isinstance(block, dict) else block


def answer_text(question: str, target, language: str, script: str, idx: int = 0, last_agent: str = "") -> str:
    """The fixed answer plus a re-ask (a different phrasing each time), in the call's language."""
    L = _lang(language)
    if question == "Q_REPEAT":
        return script
    body = persona.apply_gender(_pick(L[question], target.script_kind).format(**_facts(target)))
    return f"{body} {persona.reask(target.script_kind, language, idx, last_agent)}"


def closing_text(intent: str, routed: str, promise_iso: str | None, language: str) -> str | None:
    L = {k: persona.apply_gender(v) for k, v in _lang(language)["closing"].items()}
    if (routed or "").startswith("escalated"):
        return _say(language, "handoff") if intent != "DISPUTE" else L["dispute"]
    if intent == "PROMISE" and promise_iso:
        d = date.fromisoformat(promise_iso)
        return L["promise"].format(date=d.strftime("%d %b"))
    if intent == "PAY_NOW":
        return L["pay_now"]
    if intent == "DISPUTE":
        return L["dispute"]
    if intent == "CALLBACK":
        return L["callback"]
    if intent == "WRONG_NUMBER":
        return L["wrong_number"]
    return None


# ── Human touches (chosen by code, never by the model) ────────────────────────

_HARDSHIP = (
    "paise nahi", "paisa nahi", "job gayi", "job chali", "naukri gayi", "naukri chali", "bimar", "bimaar",
    "hospital", "medical", "salary nahi", "salary late", "salary delay", "mushkil", "lost my job", "no money",
    "sick", "hardship", "accident", "nuksan", "financial problem", "tight", "बीमार", "नौकरी", "पैसे नहीं",
    "मुश्किल", "अस्पताल",
)
_FRUSTRATED = ("pareshan", "tang aa", "bakwas", "irritat", "annoy", "bar bar", "baar baar", "kitni baar",
               "stop calling", "mat karo call", "disturb", "frustrat", "fed up")


_POLICY_LEADIN = {
    "hinglish": "Hamari policy ke mutabiq,",
    "hi": "हमारी नीति के अनुसार,",
    "en": "As per our policy,",
}


def _trim_passage(t: str, limit: int = 300) -> str:
    """The approved text itself, cut at a sentence end (used when the model's own wording fails the check)."""
    t = re.sub(r"^[^.]{0,80}\.\s+", "", t, count=1) if t[:80].count(".") and len(t) > 120 else t   # drop the "Heading." lead
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("। "))
    return (cut[: end + 1] if end > 80 else cut).strip()


# A date alone ("salary 5 tareekh ko aati hai") is not a promise. Booking one needs a commitment in
# the customer's own words, otherwise the agent asks "shall I note that date?" first.
_COMMIT = ("kar dunga", "kar dungi", "karunga", "karungi", "kar denge", "kar dungi", "de dunga", "de dungi",
           "bhej dunga", "bhej dungi", "ho jayega", "ho jaega", "ho jayegi", "pakka", "will pay", "i'll pay", "i will pay",
           "i'll make", "i will make", "will do it", "pay kar", "payment kar", "कर दूँगा", "कर दूँगी", "करूँगा", "करूँगी",
           "कर देंगे", "हो जाएगा", "पक्का")
_YES = ("haan", "han ", "ha ", "ji haan", "ji ha", "theek", "thik", "ok", "okay", "yes", "yeah", "yep", "sure", "bilkul",
        "kar lo", "kar dijiye", "note kar", "chalega", "sahi", "हाँ", "हां", "ठीक", "जी", "बिल्कुल", "सही")
_NO = ("nahi", "nahin", "no ", "mat ", "galat", "नहीं", "मत")
_NO_WORDS = {"nah", "nhi", "nai", "nope", "na"}      # stretched / short spellings ("nahh", "nope") the substring list misses
# Speech-to-text returns Hindi in Devanagari ("हा हा नोट कर लो"), which the Roman words above never match. Whole words only:
# the bare "हा" is inside "कहा" (said), so a substring test would turn "मैंने कहा था" into a yes.
_YES_DEVA_WORDS = {"हा", "हाँ", "हां", "हाँजी", "हांजी", "जी", "ठीक", "ओके", "ओके।", "यस", "बिल्कुल", "सही", "चलेगा",
                   "चलेगी", "जरूर", "ज़रूर", "पक्का"}
_YES_DEVA_PHRASES = ("नोट कर", "कर लो", "कर लीजिए", "कर दीजिए", "कर दो", "ठीक है", "ठीक रहेगा", "सही है")


def has_commitment(text: str) -> bool:
    t = voice._romanize_hints(text or "").lower()
    return any(k in t or k in (text or "").lower() for k in _COMMIT)


def is_yes(text: str) -> bool:
    t = f" {(text or '').lower().strip()} "
    deva_yes = (any(w in _YES_DEVA_WORDS for w in re.findall(r"[\u0900-\u097f]+", t))
                or any(p in t for p in _YES_DEVA_PHRASES))
    kind = short_replies.read(text)
    if kind in (short_replies.YES, short_replies.SEND):
        return True
    if kind is not None:                    # a short "no" / "wait" / "what?" is never a yes
        return False
    words = set(short_replies.tokens(text))
    no_stretched = bool(words & {short_replies._collapse(w) for w in _NO_WORDS})
    # Latin words match whole words only ("ok" is not inside "book", "yes" not inside "yesterday", "ha" not inside "kaha");
    # multi-word phrases and the Devanagari lists keep the substring test.
    latin = set(re.findall(r"[a-z]+", t))
    yes_word = any((k.strip() in latin) if " " not in k.strip() and k.isascii() else (k in t) for k in _YES)
    return (yes_word or deva_yes) and not any(k in t for k in _NO) and not no_stretched


def mentions_hardship(text: str) -> bool:
    t = voice._romanize_hints(text or "").lower()
    return any(k in t for k in _HARDSHIP)


def _soften(body: str) -> str:
    """"Bakaya amount ..." -> "bakaya amount ..." after a filler (not acronyms/numbers)."""
    if len(body) > 1 and body[0].isupper() and body[1].islower():
        return body[0].lower() + body[1:]
    return body


def _recent_leads(turns) -> set[str]:
    """Lead words of the last two agent lines - the no-repeat window."""
    agent = [t for t in turns if t.speaker == "agent"][-2:]
    return {persona.lead(t.text) for t in agent}


def effective_approach(base: str, turns) -> str:
    """The approach for THIS moment. Someone who was upset earlier in this call stays handled gently for the rest of it, even
    after they calm down: a person does not snap back to brisk the moment a customer stops sounding worried."""
    if any(t.speaker == "agent" and t.kind in ("hardship", "apology") for t in turns):
        return "gentle"
    return base


def call_approach(db: Session, call) -> str | None:
    """The approach in force on this call right now (for how a line is voiced). None when the call has no profile."""
    p = customer_profile.CustomerProfile.from_json(getattr(call, "profile_json", None))
    return effective_approach(p.approach, _turns(db, call.id)) if p is not None else None


def _last_call_fillers(db: Session, call) -> set[str]:
    """Fillers the agent used on this customer's previous call, so a returning customer does not hear the same ones in the same
    places (people vary how they greet and acknowledge from one conversation to the next)."""
    if not call.customer_ref:
        return set()
    prev = (db.query(VoiceCall).filter(VoiceCall.customer_ref == call.customer_ref, VoiceCall.id != call.id,
                                       VoiceCall.status != "in_progress").order_by(VoiceCall.created_at.desc()).first())
    if prev is None:
        return set()
    rows = _turns(db, prev.id)
    return {persona.lead(t.text) for t in rows if t.speaker == "agent" and persona.starts_with_filler(t.text)}


def with_filler(body: str, language: str, turn_no: int, customer_text: str = "", repeat: bool = False,
                avoid: set[str] | None = None, approach: str | None = None,
                agent_lines: list[str] | None = None) -> str:
    """At most ONE human touch per reply: a kind line if the customer mentioned hardship, otherwise a short acknowledgement -
    rotated by turn number and never one of the last two used. With an `approach` (a call that has a customer profile) fillers are
    also RATIONED the way a person's are: not on every line, never twice in a row, none after a one-word answer, and a gentle
    approach uses softer openers more often."""
    if repeat:
        return body
    lang = language if language in persona.LANGS else "hinglish"
    if mentions_hardship(customer_text):
        return f"{persona.apply_gender(persona.pick(persona.EMPATHY[lang], turn_no, avoid))} {body}"
    if persona.starts_with_filler(body):
        return body
    last = (agent_lines or [""])[-1]
    same_opening = bool(last) and persona.lead(body) == persona.lead(last) and not persona.starts_with_filler(last)
    if approach is not None and not same_opening and not persona.wants_touch(approach, agent_lines or [], customer_text):
        return body                      # (two lines in a row opening with the same word is the one thing a filler is for)
    pool = persona.GENTLE_ACK[lang] if approach == "gentle" else persona.ACK[lang]
    return f"{persona.apply_gender(persona.pick(pool, turn_no, avoid))} {_soften(body)}"


# ── Checking a model-written reply before it is spoken ───────────────────────

MAX_REPLY_CHARS = 320
# Never allowed, grounded or not: threats, legal steps, credit-bureau talk, guarantees.
_ALWAYS_FORBIDDEN = (
    "legal", "court", "police", "arrest", "lawyer", "vakeel", "jail", "defaulter", "blacklist", "cibil",
    "credit score", "credit report", "guarantee", "कोर्ट", "पुलिस", "कानूनी",
)
# Allowed ONLY when the same word appears in a merchant-approved passage the reply was grounded on.
# An English call must stay English: Hinglish words in a reply written for an English speaker are rejected (the checker
# otherwise only looks at the SCRIPT, and Roman Hinglish passes it). High-precision words only - none is common English.
_HINGLISH_WORDS = re.compile(
    r"\b(hai|hain|nahi|nahin|aap|aapka|aapko|aapke|hum|hamari|hamare|humari|humare|mutabiq|mutabik|kya|kab|karenge|"
    r"kijiye|dijiye|rahega|hoga|hogi|paise|paisa|theek|accha|achha|bataiye|batayein|sakte|sakta|sakti|chahiye|"
    r"karna|karni|dena|lagta|lagate|lagti)\b", re.IGNORECASE)

_NOT_A_NAME = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue", "wed", "thu",
               "fri", "sat", "sun", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
               "march", "april", "june", "july", "august", "september", "october", "november", "december", "backstop", "store"}


def invented_names(reply: str, known_text: str) -> list[str]:
    """Capitalised words in the MIDDLE of a sentence (names, places) that appear nowhere in what the agent was given. A memory
    answer that adds one ("you said you are from Mars") is making something up; rules cannot check a paraphrase, but they can
    catch that."""
    known = known_text.lower()
    out = []
    for sent in re.split(r"(?<=[.!?।])\s+", reply or ""):
        words = re.findall(r"[A-Za-z][A-Za-z']*", sent)
        for w in words[1:]:
            if w[0].isupper() and len(w) >= 3 and not w.isupper() and w.lower() not in _NOT_A_NAME and w.lower() not in known:
                out.append(w)
    return out


_MONEY_WORDS = (
    "waive", "waiver", "discount", "free of charge", "no charge", "late fee", "penalty", "penalties", "interest",
    "maaf kar den", "माफ़ कर", "ब्याज", "जुर्माना", "छूट", "डिस्काउंट", "hazaar", "hazar", "lakh", "crore",
    "thousand", "hundred",
)
_HUMAN_CLAIMS = (
    "i am a human", "i'm a human", "i am a person", "i am real", "main insaan hoon", "main ek insaan",
    "मैं इंसान हूँ", "main real hoon", "i am not a robot", "not an ai",
)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _digits(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in _NUM.findall(text or "")}


def validate_reply(text: str, target, language: str, customer_text: str = "",
                   grounding: list[str] | None = None, known_text: str = "") -> str | None:
    """None when the sentence may be spoken, otherwise the reason it may not.
    `grounding` = merchant-approved passages the reply is meant to come from: their numbers and
    their money words (fee, interest, ...) become allowed, because the merchant wrote them."""
    ground = " ".join(grounding or [])
    ground_low = ground.lower()
    t = (text or "").strip()
    if not t:
        return "empty"
    if len(t) > MAX_REPLY_CHARS:
        return "too_long"
    low = t.lower()
    if re.search(r"https?:|www\.|@|\.com|\.in/", low):
        return "contains_link"
    if any(c in t for c in "*#`_{}[]<>"):
        return "markdown_or_template"
    if any(w in low for w in _ALWAYS_FORBIDDEN):
        return "forbidden_wording"
    if any(w in low and w not in ground_low for w in _MONEY_WORDS):
        return "ungrounded_money_wording"
    if any(w in low for w in _HUMAN_CLAIMS):
        return "claims_to_be_human"
    # numbers: only the debt's own, or ones the customer just said
    allowed = ({str(int(round(target.amount_inr)))} | _digits(target.ref) | _digits(target.due)
               | _digits(customer_text) | _digits(ground) | _digits(known_text))
    allowed |= {d.lstrip("0") or "0" for d in list(allowed)}
    extra = {n for n in _digits(t) if n not in allowed and (n.lstrip("0") or "0") not in allowed}
    if extra:
        return "unknown_number"
    letters = [c for c in t if c.isalpha()]
    if letters:
        deva = sum(1 for c in letters if "\u0900" <= c <= "\u097f") / len(letters)
        if language == "hi" and deva < 0.35:
            return "wrong_script"
        if language in ("hinglish", "en") and deva > 0.2:
            return "wrong_script"
        if language == "en" and _HINGLISH_WORDS.search(low):
            return "wrong_language"
    return None


# ── One model call per turn: what they meant + a natural reply ───────────────

_TURN_TOOL = {
    "type": "function",
    "function": {
        "name": "reply_to_customer",
        "description": "Record what the customer meant and, for questions, the spoken reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": voice.INTENTS + QUESTION_INTENTS},
                "promise_date": {"type": ["string", "null"],
                                 "description": "ISO date only if the customer stated a payment date or relative day."},
                "reply": {"type": "string",
                          "description": "Your spoken reply for a QUESTION or unclear line (1-2 short sentences). "
                                         "Empty string for decisions, repeat requests and requests for a person."},
                "search_query": {"type": "string",
                                 "description": "For Q_POLICY only: the customer's question rewritten as a short, clear "
                                                "English search query for the merchant's English documents. Else empty."},
                "source_ids": {"type": "array", "items": {"type": "string"},
                               "description": "For a Q_POLICY reply: the ids (p1, p2...) of the APPROVED PASSAGES "
                                              "it came from. For a Q_MEMORY reply: the ids (m1, m2...) of the "
                                              "REMEMBERED items it came from. Empty otherwise."},
                "tone": {"type": "string", "enum": ["calm", "frustrated", "distressed"],
                         "description": "How the customer sounds: frustrated = annoyed/angry at the call; "
                                        "distressed = in personal or financial difficulty; else calm."},
            },
            "required": ["intent", "promise_date", "reply", "search_query", "source_ids", "tone"],
        },
    },
}


def _localized(h, language: str) -> str | None:
    """The merchant's own wording of this policy in the call's language, if the document has one (a "Hinglish:" /
    "Hindi:" line under the passage). English calls use the English passage itself."""
    if language == "en":
        return None
    tr = (h.extra or {}).get("translations") or {}
    return tr.get("hi" if language == "hi" else "hinglish")


def _cut(t: str, limit: int = 0) -> str:
    """The first sentences of `t` that fit in `limit` characters (default: the same cap as any spoken reply; never half a
    sentence unless one sentence is too long)."""
    limit = limit or MAX_REPLY_CHARS
    if len(t) <= limit:
        return t
    out = ""
    for s in re.split(r"(?<=[.!?।])\s+", t):
        if out and len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
    return out[:limit]


def _passage_text(h) -> str:
    """What the model is shown for a policy hit: the whole section when the hit is one piece of a longer one
    (capped), otherwise the passage. The same text is what its reply is grounded against."""
    return ((h.extra or {}).get("section") or h.text)[:900]


def _approved_facts(target, language: str) -> dict:
    L = _lang(language)
    f = _facts(target)
    out = {}
    for q in _ANSWERABLE:
        if q == "Q_REPEAT":
            continue
        out[q] = persona.apply_gender(_pick(L[q], target.script_kind).format(**f))
    return out


def _llm_turn(call, target, turns, transcript: str, today: date, knowledge=(), notes_hits=(), context=(),
              memory_lines=(), approach: str | None = None) -> dict:
    if runtime_flags.is_llm_failure_forced():
        raise RuntimeError("LLM failure forced via /debug/toggle-llm-failure")
    if not settings.groq_api_key:
        raise RuntimeError("No GROQ_API_KEY configured")
    facts = {"merchant": MERCHANT_NAME, "amount": voice._inr(target.amount_inr), "due": target.due,
             "reference": target.ref, "situation": target.script_kind, "today": today.isoformat()}
    history = "\n".join(f"{'Assistant' if t.speaker == 'agent' else 'Customer'}: {t.text}" for t in turns[-10:])
    system = (
        persona.system_prompt(call.language, approach) + "\n"
        "TASK: classify what the customer's LAST line meant; if it is a question (Q_*) or unclear, write the "
        "spoken reply.\n"
        "INTENTS: PAY_NOW (asks for / agrees to the link, or pays now), PROMISE (will pay, with or without a date), "
        "DISPUTE, WRONG_NUMBER, CALLBACK (busy, call later), REFUSED, UNCLEAR; questions: Q_AMOUNT (how much), Q_WHY "
        "(why / what is this for), Q_DUE (due date), Q_HOW_PAY (how to pay), Q_WHO (who are you / are you a robot), "
        "Q_PARTIAL (pay part), Q_REPEAT (say that again), Q_MEMORY (asks about an EARLIER CALL: what was said, "
        "promised or agreed last time), Q_POLICY (the merchant's fees, refunds, cancelling, payment "
        "methods, changing bank account, disputes process, support hours, plans), Q_OTHER (wants a person or manager, "
        "complaint, anything that cannot be answered).\n"
        "RULES for the reply: 1-2 short sentences, spoken style, no lists, no emojis, no links. Use ONLY the "
        "FACTS and APPROVED ANSWERS given; never invent or state any other amount, date, fee, interest, "
        "discount, waiver, deadline, legal step or consequence. Never threaten or pressure. Do not begin with filler "
        "words like 'acha' or 'hmm' (added separately). If the customer mentions difficulty, be kind, and you may "
        "say a person can help. If their question is not covered, say you will pass it to a person. For decisions "
        "(pay now, a promise with a date, dispute, wrong number, call back, refusal) and for Q_REPEAT or Q_OTHER "
        "leave reply empty. 'kal' = tomorrow, 'parso' = day after tomorrow. "
        "If the customer asks for the payment link or says they will pay now, that is PAY_NOW, not a question. "
        "A question or a condition ('if I pay next week will you waive it?', 'agar main der karun to kya hoga?') "
        "is NOT a PROMISE: a PROMISE is a plain commitment to pay (optionally with a date). "
        "Also report the customer's tone."
    )
    def _shown(h) -> str:
        loc = _localized(h, call.language)
        return _passage_text(h) + (f"\n    (the merchant's own wording in the call's language: {loc})" if loc else "")
    passages = "\n".join(f"[p{i + 1}] {h.title}: {_shown(h)}" for i, h in enumerate(knowledge))
    notes = "\n".join(f"- {h.text}" for h in notes_hits)
    records = "\n".join(f"- {l}" for l in context)
    user = (f"FACTS: {json.dumps(facts, ensure_ascii=False)}\n"
            f"APPROVED ANSWERS: {json.dumps(_approved_facts(target, call.language), ensure_ascii=False)}\n"
            + (f"APPROVED PASSAGES (written by the merchant). For a Q_POLICY question (fees, refunds, cancelling, "
               f"payment methods, changing the bank account, disputes, support hours, plans) answer ONLY from these, in "
               f"your own short words, and list the ids you used in source_ids. Describe what the policy SAYS in general "
               f"terms ('as per our policy...'); never promise what will or will not happen to THIS customer's "
               f"account (a fee, a waiver, a date). If a passage covers the question even partly, ANSWER from it (a short "
               f"reply in the call's language, never English words copied over). Only when none of them relates to the "
               f"question, choose Q_POLICY, leave reply empty and fill search_query (we will search again):\n{passages}\n" if passages else
               "No approved passages matched this wording yet. If the customer is asking about the merchant's policies, "
               "products or how things work, still choose Q_POLICY and fill search_query (we will search again with "
               "it); use Q_OTHER only for a request for a person, a complaint, a threat, or something unrelated.\n")
            + (f"FROM OUR RECORDS (exact; you may refer to these naturally, e.g. 'pichli baar aapne kaha tha...', only if "
               f"relevant, never read them all out):\n{records}\n" if records else "")
            + (f"CUSTOMER NOTES (from earlier calls; advisory, may be outdated; never state them as policy; use "
               f"only if clearly relevant, and gently):\n{notes}\n" if notes else "")
            + (f"REMEMBERED ABOUT THIS CUSTOMER (they agreed to us keeping this; advisory and possibly out of date). "
               f"Use a profile line [c..] unprompted only if clearly relevant and gently, never recite them, never state "
               f"them as policy. Lines tagged [c1], [c2]... are that profile; lines tagged [m1], [m2]... are from earlier "
               f"calls. For a Q_MEMORY question answer ONLY from the tagged lines, in your own short words, say which call (for example 'on your call of 12 Oct'), list the "
               f"ids you used in source_ids, and never add, guess or promise anything. If they do not answer it, choose "
               f"Q_MEMORY and leave reply empty:\n{chr(10).join(memory_lines)}\n" if memory_lines else "")
            + f"\nCONVERSATION SO FAR:\n{history}\n\nCUSTOMER'S LAST LINE: {transcript}")
    for attempt in (1, 2):
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
            json={"model": settings.llm_model,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                  "tools": [_TURN_TOOL],
                  "tool_choice": {"type": "function", "function": {"name": "reply_to_customer"}},
                  "max_tokens": 600},
            timeout=7.0)
        if resp.status_code == 400 and attempt == 1 and "tool_use_failed" in resp.text:
            continue
        break
    resp.raise_for_status()
    args = json.loads(resp.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    if args.get("intent") not in voice.INTENTS + QUESTION_INTENTS:
        raise RuntimeError(f"out-of-schema intent {args.get('intent')}")
    pd = None
    if args.get("promise_date"):
        try:
            pd = date.fromisoformat(args["promise_date"])
        except ValueError:
            pd = None
    tone = args.get("tone") if args.get("tone") in ("calm", "frustrated", "distressed") else "calm"
    ids = [str(x) for x in (args.get("source_ids") or []) if isinstance(x, (str, int))]
    return {"intent": args["intent"], "promise_date": pd, "reply": (args.get("reply") or "").strip(),
            "tone": tone, "source_ids": ids, "search_query": (args.get("search_query") or "").strip()[:200]}


def understand_turn(db: Session, call, target, turns, text: str, today: date, knowledge=(), notes_hits=(),
                    context=(), memory_ctx=None, approach: str | None = None) -> dict:
    """{intent, source, reply?, tone, source_ids?}. `reply` is present only when the model wrote one
    AND it passed validate_reply; otherwise the caller uses the fixed template (or the verbatim
    approved passage for a policy question)."""
    try:
        out = _llm_turn(call, target, turns, text, today, knowledge, notes_hits, context,
                        memory_ctx.prompt_lines() if memory_ctx is not None else (), approach)
    except Exception as e:
        write_audit_entry(db, "llm_failure_fallback", {"error": str(e)[:200], "stage": "voice_turn"})
        k = voice.keyword_classify(text, today)
        k["source"] = "keyword_fallback"
        k["tone"] = "frustrated" if any(w in text.lower() for w in _FRUSTRATED) else (
            "distressed" if mentions_hardship(text) else "calm")
        q = keyword_question(text)
        if q == "Q_OTHER" or (q and k["intent"] == "UNCLEAR"):
            k["intent"] = q
        elif q and re.search(r"\b(agar|if)\b", text.lower()) and "?" in text and k["intent"] == "PROMISE":
            k["intent"] = q          # "agar ... to ...?" is a question, not a promise
        return k
    out["source"] = "llm"
    reply = out.get("reply") or ""
    intent = out["intent"]
    known = " ".join([*context, *[h.text for h in notes_hits], memory_ctx.known_text() if memory_ctx is not None else ""])
    if intent == "Q_MEMORY":
        # A memory answer must come from the remembered lines this turn actually retrieved, and say which ones.
        has_memory = memory_ctx is not None and not memory_ctx.empty()
        reason = (validate_reply(reply, target, call.language, text, grounding=[memory_ctx.known_text()], known_text=known)
                  if reply and has_memory else "empty")
        if not reason and not (set(out.get("source_ids") or []) & memory_ctx.citable_ids):
            reason = "no_source_cited"
        if not reason and invented_names(reply, f"{known} {text} {MERCHANT_NAME}"):
            reason = "invented_name"
        if reason and reply:
            write_audit_entry(db, "voice_reply_rejected", {
                "call_id": call.id, "reason": reason, "intent": intent, "reply": reply[:200]})
        if reason:
            out["reply"] = None
    elif intent == "Q_POLICY":
        ground = [_passage_text(h) for h in knowledge] + [t for t in (_localized(h, call.language) for h in knowledge) if t]
        reason = (validate_reply(reply, target, call.language, text, grounding=ground, known_text=known)
                  if reply else "empty")
        valid_ids = {f"p{i + 1}" for i in range(len(knowledge))}
        if not reason and not (set(out.get("source_ids") or []) & valid_ids):
            reason = "no_source_cited"
        if reason:
            write_audit_entry(db, "voice_reply_rejected", {
                "call_id": call.id, "reason": reason, "intent": intent, "reply": reply[:200]})
            out["reply"] = None
    elif intent in _ANSWERABLE and intent != "Q_REPEAT" or intent == "UNCLEAR":
        reason = validate_reply(reply, target, call.language, text, known_text=known) if reply else "empty"
        if reason:
            write_audit_entry(db, "voice_reply_rejected", {
                "call_id": call.id, "reason": reason, "intent": intent, "reply": reply[:200]})
            out["reply"] = None
    else:
        out["reply"] = None
    return out


# ── The turn loop ────────────────────────────────────────────────────────────

def _turns(db: Session, call_id: str) -> list[VoiceTurn]:
    return db.query(VoiceTurn).filter(VoiceTurn.call_id == call_id).order_by(VoiceTurn.turn_no.asc()).all()


def _add(db: Session, call_id: str, speaker: str, text: str, intent: str | None = None,
         kind: str | None = None, timings: dict | None = None, sources: list | None = None) -> VoiceTurn:
    n = db.query(VoiceTurn).filter(VoiceTurn.call_id == call_id).count()
    t = VoiceTurn(call_id=call_id, turn_no=n, speaker=speaker, text=text, intent=intent, kind=kind,
                  timings_json=json.dumps(timings) if timings else None,
                  sources=json.dumps(sources) if sources else None)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def agent_turn(db: Session, call_id: str, turn_no: int) -> VoiceTurn | None:
    return db.query(VoiceTurn).filter(VoiceTurn.call_id == call_id, VoiceTurn.turn_no == turn_no,
                                      VoiceTurn.speaker == "agent").first()


def record_tts_timing(db: Session, call_id: str, turn_no: int, ms: int, cache_hit: bool) -> None:
    row = agent_turn(db, call_id, turn_no)
    if row is None:
        return
    t = json.loads(row.timings_json) if row.timings_json else {}
    t["tts_ms"], t["tts_cache_hit"] = ms, bool(cache_hit)
    row.timings_json = json.dumps(t)
    db.commit()


def agent_turn_text(db: Session, call_id: str, turn_no: int) -> str | None:
    """Text of a stored agent line (the only thing /demo/speech/tts will speak
    besides the opening script) - so the voice endpoint never takes free text."""
    t = db.query(VoiceTurn).filter(VoiceTurn.call_id == call_id, VoiceTurn.turn_no == turn_no,
                                   VoiceTurn.speaker == "agent").first()
    return t.text if t else None


def _conversation(db: Session, call_id: str) -> str:
    return "\n".join(f"{'Agent' if t.speaker == 'agent' else 'Customer'}: {t.text}" for t in _turns(db, call_id))


def _view(t: VoiceTurn) -> dict:
    return {"turn_no": t.turn_no, "text": t.text, "kind": t.kind,
            "timings": json.loads(t.timings_json) if t.timings_json else None,
            "sources": json.loads(t.sources) if t.sources else None}


def latency_stats(db: Session, last: int = 50) -> dict:
    """p50/p95 of how long the server took to think, over the last agent turns."""
    rows = (db.query(VoiceTurn).filter(VoiceTurn.speaker == "agent", VoiceTurn.timings_json.isnot(None))
            .order_by(VoiceTurn.created_at.desc()).limit(last).all())
    vals = sorted(json.loads(r.timings_json).get("server_ms") for r in rows
                  if json.loads(r.timings_json).get("server_ms") is not None)
    if not vals:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    pick = lambda q: vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]
    return {"n": len(vals), "p50_ms": pick(0.5), "p95_ms": pick(0.95)}


def _finish(db: Session, call: VoiceCall, final_text: str, now, forced_closing: str | None = None,
            force: tuple | None = None, timings: dict | None = None) -> dict:
    out = voice.finish_interactive_call(db, call.id, final_text, now, force=force)
    if not out.get("ok"):
        return out
    db.expire_all()
    call = db.query(VoiceCall).filter(VoiceCall.id == call.id).first()
    closing = forced_closing or closing_text(out.get("intent") or "", out.get("routed_to") or "",
                                             out.get("promise_date"), call.language)
    agent = None
    if closing and out.get("status") == "completed":
        agent = _view(_add(db, call.id, "agent", closing, kind="closing", timings=timings))
    # the stored transcript becomes the whole conversation (the decision above
    # was made from the decisive line only, so earlier questions cannot colour it)
    call.transcript = _conversation(db, call.id) or call.transcript
    db.commit()
    return {**out, "done": True, "agent": agent}


def _short_turn(text: str, call, target, last_agent, turns) -> dict | None:
    """A one-to-four-word reply ("haan", "nahh", "hmm", "ek minute", "kya?") read by fixed word lists instead of the model, using what
    the agent just asked. {intent, source="short_reply", ...} or None when it is not a short reply (the normal reader runs)."""
    kind = short_replies.read(text)
    if kind is None or kind == short_replies.UNSURE:
        return None
    base = {"source": "short_reply", "tone": "calm", "reply": None, "promise_date": None, "short": kind}
    last_kind = last_agent.kind if last_agent is not None else None
    if kind == short_replies.YES:
        # They agreed, but gave no date: a "yes" to "shall I send it?" is pay-now; to "by when?" it is a willing promise with no
        # date yet (the agent then asks for one). A mandate call has no link to send, so it is always the promise.
        return {**base, "intent": "PAY_NOW" if target.script_kind == "order_failed" else "PROMISE"}
    if kind == short_replies.SEND:
        return {**base, "intent": "PAY_NOW"} if target.script_kind != "mandate_failed" else None
    if kind == short_replies.NO:
        if last_kind == "confirm_date":     # "no" to "shall I note 12 Oct?": not that date - ask again, not a refusal
            return {**base, "intent": "PROMISE"}
        return {**base, "intent": "REFUSED"}
    if kind == short_replies.LATER:
        return {**base, "intent": "CALLBACK"}
    if kind == short_replies.HOLD:
        return {**base, "intent": "Q_REPEAT", "hold": True}
    if kind == short_replies.REPEAT:
        return {**base, "intent": "Q_REPEAT"}
    # ACK ("hmm", "acha", "hello", "ji"): listening, not deciding. Ask the question again, once; a second bare ack is "unclear".
    if last_kind == "reask":
        return {**base, "intent": "UNCLEAR"}
    return {**base, "intent": "Q_REPEAT", "ack": True}


def converse_turn(db: Session, call_id: str, transcript: str, now=None, stt_ms: int | None = None,
                  interrupted: bool = False) -> dict:
    t_start = time.perf_counter()
    now = now or clock.now()
    call = db.query(VoiceCall).filter(VoiceCall.id == call_id).first()
    if call is None or call.status != "in_progress":
        return {"ok": False, "error": "call_not_in_progress"}
    adapter = voice._ADAPTERS.get(call.subject_type)
    target = adapter(db, call.subject_id, now) if adapter else None
    if target is None:
        return {"ok": False, "error": "subject_missing"}

    if not _turns(db, call.id):
        _add(db, call.id, "agent", call.script_text, kind="script")
    text = (transcript or "").strip()
    turns = _turns(db, call.id)
    customer_lines = sum(1 for t in turns if t.speaker == "customer")
    last_agent = next((t for t in reversed(turns) if t.speaker == "agent"), None)
    already_clarified = last_agent is not None and last_agent.kind == "clarify"
    if interrupted and last_agent is not None:      # the customer talked over that line
        last_agent.interrupted = True
        db.commit()
    profile = customer_profile.for_call(db, call, target)
    approach = effective_approach(profile.approach, turns)
    agent_lines = [t.text for t in turns if t.speaker == "agent"]
    avoid = _recent_leads(turns) | _last_call_fillers(db, call)
    mem_trace: dict | None = None        # what the tiered memory read this turn (ids only), kept on the agent turn

    def timings(**extra) -> dict:
        out = {"server_ms": int((time.perf_counter() - t_start) * 1000), "approach": approach, **extra}
        if mem_trace:
            out["memory"] = mem_trace
        if stt_ms is not None:
            out["stt_ms"] = int(stt_ms)
        return out

    if not text:
        if already_clarified:
            return _finish(db, call, "", now)       # silence twice: no answer
        a = _add(db, call.id, "agent", _say(call.language, "clarify"), kind="clarify", timings=timings())
        return {"ok": True, "done": False, "agent": _view(a), "understood": None}

    today = (now + voice._IST).date()
    if last_agent is not None and last_agent.kind == "confirm_date" and (last_agent.intent or "").startswith("CONFIRM:"):
        offered = date.fromisoformat(last_agent.intent.split(":", 1)[1])
        said = voice.parse_promise_date(voice._romanize_hints(text).lower(), today)
        if is_yes(text) and (said is None or said == offered):    # "haan, 7 ko" names ANOTHER date: not a plain yes
            pdate = offered
            _add(db, call.id, "customer", text, intent="PROMISE")
            return _finish(db, call, text, now, force=("PROMISE", "confirmed", pdate), timings=timings())
        # anything else (a different date, a "no", a question) is handled as a normal line below
    # Look things up first (both stores at once), then let the model see what was found.
    short = _short_turn(text, call, target, last_agent, turns)
    t_ret = time.perf_counter()
    kn: list = []
    mem: list = []
    mctx = memory_retrieval.Memory()
    cp = target.contact
    full_memory = contacts.has_full_memory_consent(cp)
    if settings.rag_enabled and short is None:
        try:
            # Knowledge for everyone; the older notes only for a customer still on the "short notes" wording.
            found = retrieval.search_both(text, cp.id if (cp is not None and cp.memory_consent and not full_memory) else None)
            kn = [h for h in found["knowledge"] if retrieval.is_answerable(h)]
            mem = found["memory"]
        except Exception as e:      # retrieval must never break a call
            write_audit_entry(db, "retrieval_failed", {"call_id": call.id, "error": type(e).__name__})
        try:
            mctx = memory_retrieval.build(db, cp, text, clock.now())
        except Exception as e:
            db.rollback()
            write_audit_entry(db, "retrieval_failed", {"call_id": call.id, "error": type(e).__name__, "store": "memory"})
    mem_trace = mctx.trace or None
    retrieve_ms = int((time.perf_counter() - t_ret) * 1000)
    context = memory.structured_context(db, call.id, target)
    t_llm = time.perf_counter()
    c = short if short is not None else understand_turn(db, call, target, turns, text, today, kn, mem, context, mctx, approach)
    llm_ms = int((time.perf_counter() - t_llm) * 1000)
    if c["intent"] == "Q_POLICY" and not c.get("reply") and settings.rag_enabled and c.get("search_query"):
        # A Roman-Hinglish question rarely lands on English documents; search again with the
        # model's English rewrite, and if that finds approved passages let the model answer from them.
        t_ret2 = time.perf_counter()
        try:
            kn2 = [h for h in retrieval.search_knowledge(db, f"{c['search_query']} {text}")
                   if retrieval.is_answerable(h)]
        except Exception:
            db.rollback()
            kn2 = []
        retrieve_ms += int((time.perf_counter() - t_ret2) * 1000)
        if kn2 and [h.id for h in kn2] != [h.id for h in kn]:
            kn = kn2
            t_llm2 = time.perf_counter()
            c = understand_turn(db, call, target, turns, text, today, kn, mem, context, mctx, approach)
            llm_ms += int((time.perf_counter() - t_llm2) * 1000)
    if c["intent"] == "Q_MEMORY" and full_memory and not c.get("reply") and not mctx.recalled and settings.rag_enabled:
        # The model heard a question about an earlier call that the profile alone could not answer (and our word-cue
        # missed it): recall once, then ask again.
        t_ret3 = time.perf_counter()
        try:
            mctx = memory_retrieval.build(db, cp, text, clock.now(), force_recall=True)
            mem_trace = mctx.trace or None
        except Exception:
            db.rollback()
        retrieve_ms += int((time.perf_counter() - t_ret3) * 1000)
        if mctx.recalled:
            t_llm3 = time.perf_counter()
            c = understand_turn(db, call, target, turns, text, today, kn, mem, context, mctx, approach)
            llm_ms += int((time.perf_counter() - t_llm3) * 1000)
    intent = c["intent"]
    tone = c.get("tone") or "calm"
    _add(db, call.id, "customer", text, intent=intent)
    customer_lines += 1
    decisive = intent in ("PAY_NOW", "PROMISE", "DISPUTE", "WRONG_NUMBER")

    if ((mentions_hardship(text) or tone == "distressed") and intent in ("REFUSED", "UNCLEAR", "CALLBACK")
            and not any(t.kind == "hardship" for t in turns) and customer_lines < MAX_TURNS):
        # Someone in difficulty is not a "refusal": acknowledge, offer a date or
        # a person, once. A second such line goes to a person as usual.
        body = with_filler(_say(call.language, "hardship"), call.language, len(turns) + 1, text or "hardship",
                           avoid=avoid)
        a = _add(db, call.id, "agent", body, kind="hardship", timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_call_hardship_acknowledged", {"call_id": call.id, "turn": a.turn_no})
        return {"ok": True, "done": False, "agent": _view(a), "understood": "HARDSHIP"}

    if (c.get("short") == short_replies.NO and intent == "REFUSED" and not any(t.kind == "hardship" for t in turns)
            and customer_lines < MAX_TURNS):
        # A bare "nahi" is not yet a decision to refuse: a speech slip or a "no" to something else is common, and ending the
        # call on it would be abrupt. Ask once for a date or a person (the same line as hardship, so it never repeats); a
        # second "no" goes through the normal refusal.
        body = with_filler(_say(call.language, "hardship"), call.language, len(turns) + 1, text, avoid=avoid,
                           approach=approach, agent_lines=agent_lines)
        a = _add(db, call.id, "agent", body, kind="hardship", timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_call_soft_decline_checked", {"call_id": call.id, "turn": a.turn_no})
        return {"ok": True, "done": False, "agent": _view(a), "understood": "SOFT_NO"}

    if (tone == "frustrated" and not decisive and intent != "Q_OTHER" and not any(t.kind == "apology" for t in turns)
            and customer_lines < MAX_TURNS):
        # Annoyed, not asking: one short apology and a way out, once.
        a = _add(db, call.id, "agent", persona.apply_gender(persona.APOLOGY[
            call.language if call.language in persona.LANGS else "hinglish"]),
                 kind="apology", timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_call_apology", {"call_id": call.id, "turn": a.turn_no})
        return {"ok": True, "done": False, "agent": _view(a), "understood": "FRUSTRATED"}

    if intent == "Q_POLICY" and customer_lines < MAX_TURNS:
        next_no = len(turns) + 1
        last_text = last_agent.text if last_agent is not None else ""
        cited = [h for i, h in enumerate(kn) if f"p{i + 1}" in (c.get("source_ids") or [])]
        if c.get("reply") and cited:                       # model-written, checked against the passages
            body, kind, used = c["reply"], "answer_policy", cited
            if "?" not in body:
                body = f"{body} {persona.reask(target.script_kind, call.language, next_no, last_text)}"
        elif kn:                                           # the checked reply failed: say the approved text itself
            top = kn[0]
            lead = _POLICY_LEADIN.get(call.language, _POLICY_LEADIN["hinglish"])
            loc = _localized(top, call.language)        # read the merchant's own Hinglish / Hindi wording, not English
            spoken = _cut(loc) if loc else _trim_passage(top.text)
            body = f"{lead} {spoken} {persona.reask(target.script_kind, call.language, next_no, last_text)}"
            kind, used = "answer_policy_verbatim", [top]
        else:                                              # nothing approved covers it: a person, not a guess
            write_audit_entry(db, "voice_policy_no_match", {"call_id": call.id, "customer_words": text[:120]})
            a = _add(db, call.id, "agent", _say(call.language, "handoff"), kind="handoff",
                     timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
            out = voice.finish_interactive_call(db, call.id, text, now, force=("UNCLEAR", "handoff", None))
            if not out.get("ok"):
                return out
            db.expire_all()
            call = db.query(VoiceCall).filter(VoiceCall.id == call.id).first()
            call.transcript = _conversation(db, call.id)
            db.commit()
            return {**out, "done": True, "agent": _view(a)}
        reply = with_filler(body, call.language, next_no, text, avoid=avoid, approach=approach, agent_lines=agent_lines)
        sources = [{"title": h.title, "heading": h.extra.get("heading"), "score": round(h.score, 2)} for h in used]
        a = _add(db, call.id, "agent", reply, kind=kind, sources=sources,
                 timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_policy_answered", {
            "call_id": call.id, "turn": a.turn_no, "chunk_ids": [h.id for h in used],
            "scores": [round(h.score, 3) for h in used], "reply_source": kind})
        return {"ok": True, "done": False, "agent": _view(a), "understood": intent}

    if intent == "Q_MEMORY" and customer_lines < MAX_TURNS:
        next_no = len(turns) + 1
        last_text = last_agent.text if last_agent is not None else ""
        ids = set(c.get("source_ids") or [])
        cited = [r for r in mctx.recalled if r["mid"] in ids]
        cited_core = ids & mctx.core_ids
        summary = next((r for r in mctx.recalled if r["kind"] == "summary"), None)
        L = _lang(call.language)
        if c.get("reply") and (cited or cited_core):       # model-written, checked against what was retrieved
            body, kind, used = c["reply"], "answer_memory", cited
            if "?" not in body:
                body = f"{body} {persona.reask(target.script_kind, call.language, next_no, last_text)}"
        elif summary is not None:                          # the checked reply failed (or no model): our own note, as written
            body = (f"{L['memory_lead'].format(when=summary['when'])} {_trim_passage(summary['text'], 220)} "
                    f"{persona.reask(target.script_kind, call.language, next_no, last_text)}")
            kind, used = "answer_memory_verbatim", [summary]
        else:                                              # nothing remembered: say so, never invent
            body = f"{L['no_memory']} {persona.reask(target.script_kind, call.language, next_no, last_text)}"
            kind, used = "answer_memory_none", []
        reply = with_filler(persona.apply_gender(body), call.language, next_no, text, avoid=avoid, approach=approach,
                            agent_lines=agent_lines)
        sources = [{"title": f"Call on {r['when']}", "heading": r["kind"], "score": r["score"]} for r in used]
        sources += [{"title": "Profile note", "heading": "core", "score": 1.0} for _ in cited_core]
        a = _add(db, call.id, "agent", reply, kind=kind, sources=sources or None,
                 timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_memory_answered", {
            "call_id": call.id, "turn": a.turn_no, "reply_source": kind,
            "memory_ids": [r["id"] for r in used], "episode_ids": sorted({r["episode_id"] for r in used}),
            "profile_lines": len(cited_core)})
        return {"ok": True, "done": False, "agent": _view(a), "understood": intent}

    if intent in _ANSWERABLE and customer_lines < MAX_TURNS:
        next_no = len(turns) + 1       # the customer line was just stored; the agent line follows it
        last_text = last_agent.text if last_agent is not None else ""
        if c.get("hold"):                                   # "ek minute": wait, say so, do not repeat the whole script
            body, kind = _say(call.language, "hold"), "hold"
        elif c.get("ack"):                                  # "hmm" / "hello": still listening - just the question again
            body, kind = persona.reask(target.script_kind, call.language, next_no, last_text), "reask"
        elif c.get("reply"):
            body = c["reply"]
            if "?" not in body and "？" not in body:
                body = f"{body} {persona.reask(target.script_kind, call.language, next_no, last_text)}"
            kind = "answer_generated"
        else:
            body = answer_text(intent, target, call.language, call.script_text, next_no, last_text)
            kind = "answer"
        reply = (body if kind == "hold"
                 else with_filler(body, call.language, next_no, text, repeat=(intent == "Q_REPEAT" and kind != "reask"), avoid=avoid,
                                  approach=approach, agent_lines=agent_lines))
        a = _add(db, call.id, "agent", reply, kind=kind, timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_call_question_answered", {
            "call_id": call.id, "question": intent, "turn": a.turn_no, "intent_source": c["source"],
            "reply_source": "model_checked" if kind == "answer_generated" else "fixed_template"})
        return {"ok": True, "done": False, "agent": _view(a), "understood": intent}

    if intent == "UNCLEAR" and not already_clarified and customer_lines < MAX_TURNS:
        a = _add(db, call.id, "agent", _say(call.language, "clarify"), kind="clarify",
                 timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        return {"ok": True, "done": False, "agent": _view(a), "understood": intent}

    if intent in QUESTION_INTENTS or intent == "UNCLEAR":
        # Q_OTHER, a question once the turn budget is spent, or a second
        # unclear reply: a person takes over. A DECISION is never turned away
        # by the budget - it always goes through the normal finish below.
        reason = ("question_out_of_scope" if intent == "Q_OTHER"
                  else "unclear_twice" if intent == "UNCLEAR" else "turn_limit")
        write_audit_entry(db, "voice_call_handed_to_person", {
            "call_id": call.id, "reason": reason, "turn": customer_lines})
        a = _add(db, call.id, "agent", _say(call.language, "handoff"), kind="handoff",
                 timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        out = voice.finish_interactive_call(db, call.id, text, now, force=("UNCLEAR", "handoff", None))
        if not out.get("ok"):
            return out
        db.expire_all()
        call = db.query(VoiceCall).filter(VoiceCall.id == call.id).first()
        call.transcript = _conversation(db, call.id)
        db.commit()
        return {**out, "done": True, "agent": _view(a)}

    # The turn call already understood the decision: reuse it (one model call
    # per turn, and no second classification that could disagree with the first)
    # with the same date check the single-shot call applies.
    pdate = voice.settle_promise_date(intent, c["source"], c.get("promise_date"), text, today)
    # Unsure about the date -> ask the customer to say it again, rather than guess or hand a willing customer to a
    # person. "Unsure" = no readable date, OR the model's date disagrees with what the words say (then the words
    # win, and the customer is asked to confirm that date). Up to two asks; a third miss goes to a person.
    said = voice.parse_promise_date(voice._romanize_hints(text).lower(), today)
    disagree = intent == "PROMISE" and c.get("promise_date") is not None and said is not None and c["promise_date"] != said
    if disagree:
        write_audit_entry(db, "voice_call_date_disagreement", {
            "call_id": call.id, "model_date": c["promise_date"].isoformat(), "words_date": said.isoformat()})
        pdate = said
    asks = sum(1 for t in turns if t.speaker == "agent" and t.kind in ("ask_date", "ask_date_again"))
    if intent == "PROMISE" and pdate is None and asks < 2:
        # They are willing but gave no date. If they once told us when money usually arrives, offer THAT date (they confirm it; it
        # is judged by the same promise rules as any other) instead of asking again. Offered once: a "no" must not loop.
        usual = None
        if asks == 0 and profile.pay_day and not any(t.kind == "confirm_date" for t in turns):
            max_days = (runtime_flags.get_promise_max_days_b2b() if target.subject_type == "invoice"
                        else runtime_flags.get_promise_max_days_b2c())
            usual = customer_profile.suggested_date(profile.pay_day, today, max_days)
        if usual is not None:
            ask = persona.apply_gender(persona.USUAL_DATE[persona._lang(call.language)]).format(
                day=profile.pay_day, date=usual.strftime("%d %b"))
            a = _add(db, call.id, "agent", ask, intent=f"CONFIRM:{usual.isoformat()}", kind="confirm_date",
                     timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
            write_audit_entry(db, "voice_call_usual_date_offered", {
                "call_id": call.id, "turn": a.turn_no, "date": usual.isoformat(), "source": "remembered_pay_day"})
            return {"ok": True, "done": False, "agent": _view(a), "understood": "CONFIRM_DATE"}
        key = "ask_date" if asks == 0 else "ask_date_again"
        line = _say(call.language, key)
        if key == "ask_date":
            line = persona.ask_date_line(approach, call.language, line, profile.partial_before)
        a = _add(db, call.id, "agent", line, kind=key,
                 timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_call_date_asked", {"call_id": call.id, "turn": a.turn_no, "ask_no": asks + 1})
        return {"ok": True, "done": False, "agent": _view(a), "understood": "ASK_DATE"}
    answered_directly = (last_agent is not None and "?" in last_agent.text and len(text.split()) <= 5)   # "kal tak." to "kab tak?"
    if intent == "PROMISE" and pdate is not None and (disagree or not (has_commitment(text) or answered_directly)):
        # They named a date but did not commit ("salary 5 tareekh ko aati hai"): confirm before booking.
        ask = _say(call.language, "confirm_date").format(date=pdate.strftime("%d %b"))
        a = _add(db, call.id, "agent", ask, intent=f"CONFIRM:{pdate.isoformat()}", kind="confirm_date",
                 timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))
        write_audit_entry(db, "voice_call_date_confirmation_asked", {
            "call_id": call.id, "turn": a.turn_no, "date": pdate.isoformat()})
        return {"ok": True, "done": False, "agent": _view(a), "understood": "CONFIRM_DATE"}
    return _finish(db, call, text, now, force=(intent, c["source"], pdate),
                   timings=timings(llm_ms=llm_ms, retrieve_ms=retrieve_ms))

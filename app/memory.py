"""
What the assistant remembers about a customer (RAG #2) - and what it deliberately does not.

Two kinds of recall, kept apart:

1. Structured facts from OUR OWN RECORDS (`structured_context`): earlier calls on this contact
   and what came of them, promises on this debt and whether they were kept. Exact, no model
   involved, and it is the debt's own history - no extra consent needed to use it.
2. Notes (`customer_memory`, vector-searched): at most 3 short, scrubbed lines written after a
   call - when money usually arrives, a convenient time to call, a hardship FLAG (never the
   details). Fuzzy and advisory. Written ONLY if the customer ticked the separate
   "remember my preferences" box; every query is filtered to that one contact (never shared
   across customers); they expire (MEMORY_RETENTION_DAYS) and can be deleted at any time.

What is never stored: phone numbers, e-mail, long digit runs, names, anything about health,
family, religion or legal trouble. Audit entries record ids and counts, never note content.
"""
import json
import logging
import re
import threading
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import String, and_, cast, exists, or_
from sqlalchemy.orm import Session

from app import clock, embeddings, retrieval
from app.audit import write_audit_entry
from app.config import settings
from app.database import SessionLocal, get_db
from app.merchant_auth import require_merchant
from app.models import ContactPoint, PaymentPromise, VoiceCall, VoiceTurn
from app.rag_models import (ConversationEpisode, CustomerFact, CustomerMemory, EpisodeChunk,
                            MemoryLink)

logger = logging.getLogger(__name__)
router = APIRouter()

KINDS = ("preference", "stated_constraint", "callback_time", "hardship_flag", "objection")
_ONE_PER_KIND = {"preference", "callback_time", "hardship_flag"}      # the newest replaces the older
MAX_NOTES_PER_CALL = 3
MAX_NOTE_CHARS = 140

_SENSITIVE = (
    "hospital", "cancer", "surgery", "medical", "medicine", "health", "illness", "disease", "pregnan", "bimar",
    "bimaar", "religion", "caste", "church", "mosque", "temple", "divorce", "death", "passed away", "funeral",
    "police", "jail", "court", "arrest", "lawyer", "legal", "suicide", "abuse", "loan shark", "affair",
    "अस्पताल", "बीमार", "कैंसर", "मौत", "तलाक", "पुलिस", "कोर्ट",
)
_PHONE = re.compile(r"(?:\+?\d[\d\s\-]{7,}\d)")
_EMAIL = re.compile(r"\S+@\S+")


def scrub(text: str, limit: int = MAX_NOTE_CHARS) -> str | None:
    """A note safe to keep, or None. Strips contact details; refuses sensitive subjects."""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        return None
    low = t.lower()
    if any(w in low for w in _SENSITIVE):
        return None
    t = _EMAIL.sub("[removed]", _PHONE.sub("[removed]", t))
    t = re.sub(r"\d{6,}", "[removed]", t)
    return t[:limit]


# ── Reading ──────────────────────────────────────────────────────────────────

def _ago(dt) -> str:
    days = max(0, (clock.now().date() - dt.date()).days) if dt else 0
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


def _outcome_phrase(routed: str | None, status: str) -> str:
    r = routed or ""
    if status == "no_answer" or r.startswith("callback_scheduled"):
        return "did not pick up / asked for a call later"
    if r.startswith("promise_created"):
        return f"promised to pay on {r.split(':', 1)[1]}" if ":" in r else "made a promise to pay"
    if r.startswith("payment_link_sent"):
        return "asked for the payment link"
    if r.startswith("voice_stopped_wrong_number"):
        return "said it was a wrong number"
    if r.startswith("invoice_disputed") or "unauthorised" in r:
        return "disputed the charge"
    if r.startswith("escalated"):
        return "was handed to a person"
    return "spoke with us"


def structured_context(db: Session, call_id: str | None, target) -> list[str]:
    """Exact facts from our own records (no model, no extra consent: it is this debt's history)."""
    lines: list[str] = []
    cp = target.contact
    if cp is not None:
        q = db.query(VoiceCall).filter(VoiceCall.customer_ref == cp.id, VoiceCall.status != "in_progress")
        if call_id:
            q = q.filter(VoiceCall.id != call_id)
        prev = q.order_by(VoiceCall.created_at.desc()).limit(3).all()
        for c in prev:
            lines.append(f"{_ago(c.created_at)}: on an earlier call the customer {_outcome_phrase(c.routed_to, c.status)}.")
    promises = (db.query(PaymentPromise)
                .filter(PaymentPromise.subject_type == target.subject_type, PaymentPromise.subject_id == target.subject_id)
                .order_by(PaymentPromise.created_at.desc()).limit(3).all())
    for p in promises:
        if p.status in ("kept", "partially_kept", "broken"):
            lines.append(f"A promise to pay by {p.promised_date.strftime('%d %b')} was {p.status.replace('_', ' ')}.")
    return lines[:4]


def notes_for_contact(db: Session, contact_id: str) -> list[dict]:
    rows = (db.query(CustomerMemory)
            .filter(CustomerMemory.contact_id == contact_id, CustomerMemory.expires_at > clock.now())
            .order_by(CustomerMemory.created_at.desc()).all())
    return [{"id": r.id, "kind": r.kind, "text": r.text, "created_at": r.created_at, "expires_at": r.expires_at}
            for r in rows]


# ── Writing ──────────────────────────────────────────────────────────────────

_SALARY = re.compile(r"(?:salary|tankhwah|tankha|paise?\s+aate).{0,30}?\b(\d{1,2})\s*(?:tareekh|tarikh|date|th|ko)\b"
                     r"|\b(\d{1,2})\s*(?:tareekh|tarikh|date)\s*(?:ko)?.{0,20}?salary", re.IGNORECASE)
_TIME = (("evening", ("shaam", "evening")), ("morning", ("subah", "morning")), ("afternoon", ("dopahar", "afternoon")))


def _rule_notes(customer_lines: list[str], routed: str | None) -> list[dict]:
    from app import voice_chat
    joined = " ".join(customer_lines)
    low = joined.lower()
    out: list[dict] = []
    m = _SALARY.search(low)
    if m:
        day = m.group(1) or m.group(2)
        out.append({"kind": "stated_constraint", "text": f"Said money usually arrives around day {day} of the month."})
    for label, words in _TIME:
        if any(w in low for w in words) and any(k in low for k in ("call", "baat", "phone")):
            out.append({"kind": "callback_time", "text": f"Prefers to be called in the {label}."})
            break
    if voice_chat.mentions_hardship(joined):
        out.append({"kind": "hardship_flag", "text": "Mentioned a money difficulty recently; be gentle."})
    if routed and ("invoice_disputed" in routed or "unauthorised" in routed):
        out.append({"kind": "objection", "text": "Disputed the charge on an earlier call."})
    return out


_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "save_notes",
        "description": "Short notes worth remembering for future payment calls with this customer.",
        "parameters": {
            "type": "object",
            "properties": {"notes": {"type": "array", "maxItems": MAX_NOTES_PER_CALL, "items": {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                               "text": {"type": "string", "description": f"<= {MAX_NOTE_CHARS} chars, plain language"}},
                "required": ["kind", "text"]}}},
            "required": ["notes"],
        },
    },
}


def _llm_notes(customer_lines: list[str]) -> list[dict]:
    if not settings.groq_api_key:
        raise RuntimeError("no key")
    prompt = (
        "You write notes for a payment-recovery assistant to use on a FUTURE call with the same customer. From the "
        "customer's lines below, keep ONLY practical facts they stated about paying or being reached: when money "
        "usually arrives (a day of the month), a convenient time to call, how they prefer to be contacted, whether "
        "they disputed the charge, or a gentle-handling flag if they mentioned a money difficulty (the flag only, no "
        "details). NEVER record health, family, religion, legal matters, names, phone numbers or e-mail. At most "
        f"{MAX_NOTES_PER_CALL} notes, each under {MAX_NOTE_CHARS} characters. If nothing qualifies return an empty list.\n\n"
        "CUSTOMER LINES:\n" + "\n".join(f"- {l}" for l in customer_lines))
    r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
                   json={"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
                         "tools": [_NOTE_TOOL], "tool_choice": {"type": "function", "function": {"name": "save_notes"}},
                         "max_tokens": 400}, timeout=10)
    r.raise_for_status()
    args = json.loads(r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    return [n for n in (args.get("notes") or []) if isinstance(n, dict) and n.get("kind") in KINDS][:MAX_NOTES_PER_CALL]


def write_notes(db: Session, cp: ContactPoint, call_id: str, notes: list[dict]) -> int:
    """Store scrubbed notes (consent re-checked here, at the last moment). Returns how many."""
    if not (settings.rag_enabled and cp.memory_consent):
        return 0
    clean: list[tuple[str, str]] = []
    for n in notes:
        t = scrub(n.get("text", ""))
        if t and n.get("kind") in KINDS:
            clean.append((n["kind"], t))
    if not clean:
        return 0
    vectors = embeddings.embed([t for _, t in clean]) if embeddings.available() else [None] * len(clean)
    expires = clock.now() + timedelta(days=settings.memory_retention_days)
    existing = {(r.kind, r.text.lower()) for r in db.query(CustomerMemory).filter(
        CustomerMemory.contact_id == cp.id).all()}
    saved = 0
    for (kind, t), vec in zip(clean, vectors):
        if (kind, t.lower()) in existing:
            continue
        if kind in _ONE_PER_KIND:
            db.query(CustomerMemory).filter(CustomerMemory.contact_id == cp.id, CustomerMemory.kind == kind).delete()
        db.add(CustomerMemory(merchant_id=settings.default_merchant_id, contact_id=cp.id, kind=kind, text=t,
                              source_call_id=call_id, embedding=vec, expires_at=expires))
        saved += 1
    db.commit()
    if saved:
        write_audit_entry(db, "customer_memory_written", {"contact_id": cp.id, "call_id": call_id, "notes": saved})
    return saved


def after_call(call_id: str) -> None:
    """Background: remember a finished call (own DB session; never raises).

    Consent decides how much: the current (v2) wording keeps the tiered memory - episode, word-for-word
    chunks, facts (`memory_write.record_call`); someone who only agreed to the older "short notes" wording
    keeps getting exactly those notes and nothing more."""
    db = SessionLocal()
    try:
        call = db.query(VoiceCall).filter(VoiceCall.id == call_id).first()
        if call is None or not call.customer_ref:
            return
        cp = db.query(ContactPoint).filter(ContactPoint.id == call.customer_ref).first()
        if cp is None or not cp.memory_consent:
            return                       # nothing is even summarised without consent
        from app import contacts, memory_write      # local: both import this module
        if contacts.has_full_memory_consent(cp):
            memory_write.record_call(db, call, cp)
            return
        lines = [t.text for t in db.query(VoiceTurn).filter(VoiceTurn.call_id == call_id, VoiceTurn.speaker == "customer")
                 .order_by(VoiceTurn.turn_no).all()]
        if not lines:
            return
        try:
            notes = _llm_notes(lines)
        except Exception:
            notes = []
        notes = notes or _rule_notes(lines, call.routed_to)
        write_notes(db, cp, call_id, notes)
    except Exception as e:
        logger.warning("memory summarising failed: %s", type(e).__name__)
        db.rollback()
    finally:
        db.close()


def schedule_after_call(call_id: str) -> None:
    threading.Thread(target=after_call, args=(call_id,), daemon=True, name="memory-after-call").start()


# ── Forgetting ───────────────────────────────────────────────────────────────

def forget(db: Session, contact_id: str, reason: str = "requested") -> int:
    """Hard-delete everything remembered about one contact - notes, episodes, the word-for-word
    chunks (their embeddings are columns on the same rows), facts and links - in ONE transaction,
    so a failure can never leave half a customer behind. Returns the total rows removed."""
    counts = {
        "notes": db.query(CustomerMemory).filter(CustomerMemory.contact_id == contact_id).delete(),
        "chunks": db.query(EpisodeChunk).filter(EpisodeChunk.contact_id == contact_id).delete(),
        "episodes": db.query(ConversationEpisode).filter(ConversationEpisode.contact_id == contact_id).delete(),
        "facts": db.query(CustomerFact).filter(CustomerFact.contact_id == contact_id).delete(),
        "links": db.query(MemoryLink).filter(MemoryLink.contact_id == contact_id).delete(),
    }
    db.commit()
    total = sum(counts.values())
    if total:
        write_audit_entry(db, "customer_memory_deleted", {"contact_id": contact_id, "reason": reason, **counts})
    return total


def sweep_expired(db: Session) -> int:
    """Delete what has outlived its retention, plus facts nobody has confirmed for half the retention
    period that were never important. Links die with the item they point at."""
    now = clock.now()
    stale_before = now - timedelta(days=max(1, settings.memory_retention_days // 2))
    counts = {
        "notes": db.query(CustomerMemory).filter(CustomerMemory.expires_at <= now).delete(),
        "chunks": db.query(EpisodeChunk).filter(EpisodeChunk.expires_at <= now).delete(),
        "episodes": db.query(ConversationEpisode).filter(ConversationEpisode.expires_at <= now).delete(),
        "facts": db.query(CustomerFact).filter(or_(
            CustomerFact.expires_at <= now,
            and_(CustomerFact.status == "active", CustomerFact.importance < 0.4,
                 CustomerFact.last_confirmed <= stale_before))).delete(synchronize_session=False),
    }
    # a link whose episode or fact is gone is meaningless (promise / subject ends are not memory items)
    def _dangling(end: str):
        type_col, id_col = getattr(MemoryLink, f"{end}_type"), getattr(MemoryLink, f"{end}_id")
        return or_(
            and_(type_col == "episode", ~exists().where(cast(ConversationEpisode.id, String) == id_col)),
            and_(type_col == "fact", ~exists().where(cast(CustomerFact.id, String) == id_col)))
    counts["links"] = db.query(MemoryLink).filter(or_(_dangling("src"), _dangling("dst"))).delete(synchronize_session=False)
    db.commit()
    total = sum(counts.values())
    if total:
        write_audit_entry(db, "customer_memory_expired", counts)
    return total


@router.delete("/contacts/{contact_id}/memory", dependencies=[Depends(require_merchant)])
def forget_contact(contact_id: str, db: Session = Depends(get_db)):
    """Merchant-side "forget this customer" (also the right to be forgotten)."""
    cp = db.query(ContactPoint).filter(ContactPoint.id == contact_id).first()
    if cp is None:
        raise HTTPException(404, "Unknown contact")
    return {"deleted": forget(db, contact_id, reason="merchant_request")}

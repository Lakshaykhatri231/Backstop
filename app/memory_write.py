"""
Tiered call memory - the WRITE side. Runs when a call ends, only for a customer who agreed to the
current (v2) memory wording (`contacts.has_full_memory_consent`).

Two steps, like a fast reflex and a slower "sleep-time" pass:

1. FAST (`record_episode`, no model, a few ms): freeze the call as an *episode*, store the whole
   conversation WORD FOR WORD in 4-turn windows (`episode_chunks`, embedded so it can be searched
   later), and add typed links - which call came before, which debt it was about, whether a broken
   promise led to this call. Nothing here can fail because a model is down.
2. SLOW (`summarise_episode`, one forced-tool model call): a 2-3 sentence summary, the tone, key events,
   an importance score and a few durable *facts* (salary day, best time to call...). Falls back to
   rules + an extractive summary and writes an `llm_failure_fallback` audit entry - never silent.

The model never decides what is written: it only fills a fixed schema, every value goes through
`memory.scrub`, and the fact KEYS are an allow-list. Verbatim chunks are the one thing that is not
scrubbed - they are a copy of what was said, and that is the point of them. A changed fact
supersedes the old one (kept, with `valid_to` and a `supersedes` link) instead of overwriting it.
Audit entries carry ids and counts, never text.
"""
import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app import clock, embeddings, memory
from app.audit import write_audit_entry
from app.config import settings
from app.models import ContactPoint, PaymentPromise, VoiceCall, VoiceTurn
from app.rag_models import ConversationEpisode, CustomerFact, EpisodeChunk, MemoryLink

logger = logging.getLogger(__name__)

# One ACTIVE row per key. The model may only choose among these.
FACT_KEYS = ("salary_day", "preferred_call_time", "preferred_language", "hardship_flag",
             "objection_dispute", "commitment_style", "preference", "stated_constraint")
MAX_SUMMARY_CHARS = 400
MAX_EVENTS = 4
MAX_EVENT_CHARS = 100

# memory._rule_notes speaks the old note kinds; these are the matching fact keys.
_NOTE_KIND_TO_KEY = {"callback_time": "preferred_call_time", "hardship_flag": "hardship_flag",
                     "objection": "objection_dispute", "stated_constraint": "stated_constraint",
                     "preference": "preference"}


# ── Pure helpers (no database, no network: unit-tested) ──────────────────────

def speaker_label(speaker: str) -> str:
    return "Agent" if speaker == "agent" else "Customer"


def build_windows(turns: list[tuple[int, str, str]], size: int | None = None) -> list[dict]:
    """`turns` = [(turn_no, speaker, text)] in order. Windows of `size` turns that overlap by one turn
    (so a question and its answer are never cut apart), each rendered "Agent: ...\\nCustomer: ...".
    The text is the turns exactly as spoken - joined, never rewritten."""
    size = max(2, size or settings.memory_chunk_turns)
    out: list[dict] = []
    i, n = 0, len(turns)
    while n:
        win = turns[i:i + size]
        out.append({"seq": len(out), "turn_from": win[0][0], "turn_to": win[-1][0],
                    "text": "\n".join(f"{speaker_label(sp)}: {tx}" for _, sp, tx in win)})
        if i + size >= n:
            break
        i += size - 1
    return out


def norm_value(v: str) -> str:
    return " ".join((v or "").lower().split())


def fact_action(active_value: str | None, new_value: str) -> str:
    """create | confirm (same value again) | supersede (a different value for the same key)."""
    if active_value is None:
        return "create"
    return "confirm" if norm_value(active_value) == norm_value(new_value) else "supersede"


def clamp01(x, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def rule_importance(outcome: str | None, hardship: bool) -> float:
    o = outcome or ""
    if "dispute" in o or "unauthorised" in o:
        return 0.8
    if hardship:
        return 0.7
    if o.startswith("promise_created"):
        return 0.7
    if o.startswith("voice_stopped_wrong_number"):
        return 0.3
    return 0.5


def extractive_summary(customer_lines: list[str], outcome: str | None, status: str = "completed") -> str:
    """A plain summary with no model: how the call ended + the first thing(s) the customer said (scrubbed)."""
    parts = [f"The customer {memory._outcome_phrase(outcome, status)}."]
    from app import voice_chat      # local: voice_chat imports memory_retrieval, which is imported with this package
    # a line about a money difficulty is reduced to a flag elsewhere (rule_facts); its details are never quoted here
    said = [s for s in (memory.scrub(l, limit=120) for l in customer_lines
                        if not voice_chat.mentions_hardship(l)) if s][:2]
    if said:
        parts.append("They said: " + " / ".join(f'"{s}"' for s in said))
    return " ".join(parts)[:MAX_SUMMARY_CHARS]


def note_fact_key(kind: str, note_text: str) -> str | None:
    """The fact key for an old-style note (`customer_memory`), or None for a kind we do not carry over."""
    key = _NOTE_KIND_TO_KEY.get(kind)
    low = (note_text or "").lower()
    if key == "stated_constraint" and "day" in low and "arrives" in low:
        key = "salary_day"            # memory._rule_notes words a salary day exactly like this
    return key


def rule_facts(customer_lines: list[str], routed: str | None) -> list[dict]:
    """The rule-based fallback, as facts: reuses memory._rule_notes so the regexes live in one place."""
    facts = []
    for n in memory._rule_notes(customer_lines, routed):
        key = note_fact_key(n["kind"], n["text"])
        if key:
            facts.append({"key": key, "value": n["text"], "confidence": 0.6})
    return facts


def clean_facts(raw: list) -> list[dict]:
    """Allow-listed keys, scrubbed values, clamped scores; one value per key (the first wins)."""
    out, seen = [], set()
    for f in raw or []:
        if not isinstance(f, dict) or f.get("key") not in FACT_KEYS or f["key"] in seen:
            continue
        v = memory.scrub(str(f.get("value", "")))
        if not v:
            continue
        seen.add(f["key"])
        out.append({"key": f["key"], "value": v, "confidence": clamp01(f.get("confidence"), 0.7)})
    return out


def clean_episode(raw: dict) -> dict:
    """Validate a model reply to the save_episode tool. Anything unusable is dropped, not guessed."""
    events = [e for e in (memory.scrub(str(x), limit=MAX_EVENT_CHARS) for x in (raw.get("key_events") or [])
                          if isinstance(x, (str, int, float))) if e][:MAX_EVENTS]
    return {
        "summary": memory.scrub(str(raw.get("summary") or ""), limit=MAX_SUMMARY_CHARS),
        "tone_arc": memory.scrub(str(raw.get("tone_arc") or ""), limit=60),
        "key_events": events,
        "importance": clamp01(raw.get("importance"), 0.5),
        "facts": clean_facts(raw.get("facts") or []),
    }


# ── The model step (fills a fixed schema; decides nothing else) ──────────────

_EPISODE_TOOL = {
    "type": "function",
    "function": {
        "name": "save_episode",
        "description": "Summarise one finished payment-recovery call so a future call with the same customer can use it.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "2-3 plain sentences: why we called, what the customer said, how it ended"},
                "tone_arc": {"type": "string", "description": "how the customer's mood moved, e.g. 'tense -> relieved'"},
                "key_events": {"type": "array", "maxItems": MAX_EVENTS, "items": {"type": "string"}},
                "importance": {"type": "number", "description": "0 (routine) to 1 (a dispute, hardship, or a promise)"},
                "facts": {"type": "array", "maxItems": 4, "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string", "enum": list(FACT_KEYS)},
                                   "value": {"type": "string", "description": f"<= {memory.MAX_NOTE_CHARS} chars"},
                                   "confidence": {"type": "number"}},
                    "required": ["key", "value"]}},
            },
            "required": ["summary", "importance", "facts"],
        },
    },
}


def _llm_episode(conversation: str) -> dict:
    if not settings.groq_api_key:
        raise RuntimeError("no key")
    prompt = (
        "You keep memory for a payment-recovery phone assistant. Below is one finished call. Fill the tool: a short "
        "plain summary, how the customer's mood moved, up to four key events, an importance score, and durable FACTS "
        "the CUSTOMER stated about paying or being reached (when money usually arrives, a convenient time to call, a "
        "preferred language, whether they disputed the charge, how they like to commit, a gentle-handling flag if they "
        "mentioned a money difficulty - the flag only, never the details). NEVER record health, family, religion, legal "
        "matters, names, phone numbers or e-mail, in the summary or the facts; if the customer mentioned a money "
        "difficulty, the summary says only THAT they did, never what it was. Do not invent anything not said. If "
        "there is no durable fact, return an empty list.\n\nCALL:\n" + conversation)
    r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
                   json={"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
                         "tools": [_EPISODE_TOOL],
                         "tool_choice": {"type": "function", "function": {"name": "save_episode"}},
                         "max_tokens": 600}, timeout=12)
    r.raise_for_status()
    args = r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    return clean_episode(json.loads(args) if isinstance(args, str) else args)


# ── Fast path ────────────────────────────────────────────────────────────────

def _expires() -> datetime:
    return clock.now() + timedelta(days=settings.memory_retention_days)


def _link(db: Session, cp: ContactPoint, src: tuple[str, str], dst: tuple[str, str], relation: str) -> None:
    db.add(MemoryLink(merchant_id=settings.default_merchant_id, contact_id=cp.id, src_type=src[0], src_id=str(src[1]),
                      dst_type=dst[0], dst_id=str(dst[1]), relation=relation))


def record_episode(db: Session, call: VoiceCall, cp: ContactPoint) -> ConversationEpisode | None:
    """Fast path. Idempotent per call. Caller has already checked the v2 consent; re-checked here."""
    from app import contacts
    if not (settings.rag_enabled and contacts.has_full_memory_consent(cp)):
        return None
    existing = db.query(ConversationEpisode).filter(ConversationEpisode.call_id == call.id).first()
    if existing is not None:
        return existing
    turns = [(t.turn_no, t.speaker, t.text) for t in
             db.query(VoiceTurn).filter(VoiceTurn.call_id == call.id).order_by(VoiceTurn.turn_no).all()]
    if not turns:
        return None
    prev = (db.query(ConversationEpisode).filter(ConversationEpisode.contact_id == cp.id)
            .order_by(ConversationEpisode.started_at.desc()).first())

    ep = ConversationEpisode(
        merchant_id=settings.default_merchant_id, contact_id=cp.id, call_id=call.id,
        subject_type=call.subject_type, subject_id=str(call.subject_id), started_at=call.created_at or clock.now(),
        outcome=call.routed_to, expires_at=_expires())
    db.add(ep)
    db.flush()

    windows = build_windows(turns)
    vectors = (embeddings.embed([w["text"] for w in windows]) if embeddings.available() else [None] * len(windows))
    for w, vec in zip(windows, vectors):
        db.add(EpisodeChunk(merchant_id=settings.default_merchant_id, contact_id=cp.id, episode_id=ep.id,
                            seq=w["seq"], turn_from=w["turn_from"], turn_to=w["turn_to"], text=w["text"],
                            embedding=vec, embedding_model=settings.embedding_model if vec else None,
                            expires_at=ep.expires_at))

    # links - all derived from records, none from the model
    subject = f"{call.subject_type}:{call.subject_id}"
    _link(db, cp, ("episode", ep.id), ("subject", subject), "entity")
    promise = (db.query(PaymentPromise).filter(
        PaymentPromise.subject_type == call.subject_type, PaymentPromise.subject_id == str(call.subject_id),
        PaymentPromise.captured_via == "voice", PaymentPromise.created_at >= (call.created_at or clock.now()))
        .order_by(PaymentPromise.created_at.desc()).first())
    if promise is not None:
        _link(db, cp, ("episode", ep.id), ("promise", promise.id), "entity")
    if prev is not None:
        _link(db, cp, ("episode", prev.id), ("episode", ep.id), "temporal_next")
        # a promise from the previous call on this same debt that was then broken led to this call
        if (prev.outcome or "").startswith("promise_created") and prev.subject_id == str(call.subject_id) and \
                db.query(PaymentPromise).filter(PaymentPromise.subject_type == call.subject_type,
                                                PaymentPromise.subject_id == str(call.subject_id),
                                                PaymentPromise.status == "broken").first() is not None:
            _link(db, cp, ("episode", prev.id), ("episode", ep.id), "causal")
    db.commit()
    write_audit_entry(db, "episode_recorded", {"contact_id": cp.id, "call_id": call.id, "episode_id": ep.id,
                                               "chunks": len(windows), "embedded": sum(1 for v in vectors if v)})
    return ep


# ── Facts ────────────────────────────────────────────────────────────────────

def upsert_fact(db: Session, cp: ContactPoint, key: str, value: str, confidence: float,
                importance: float, episode_id: str | None) -> str:
    """Returns create | confirm | supersede. A different value never overwrites: the old row is kept
    (status superseded, valid_to set) and a `supersedes` link records the change."""
    now = clock.now()
    active = db.query(CustomerFact).filter(CustomerFact.contact_id == cp.id, CustomerFact.key == key,
                                           CustomerFact.status == "active").first()
    action = fact_action(active.value if active else None, value)
    if action == "confirm":
        active.last_confirmed = now
        active.confidence = min(1.0, (active.confidence or 0.7) + 0.1)
        active.expires_at = _expires()
        return action
    vec = None
    if embeddings.available():
        vec = embeddings.embed([f"{key.replace('_', ' ')}: {value}"])[0]
    new = CustomerFact(merchant_id=settings.default_merchant_id, contact_id=cp.id, key=key, value=value,
                       confidence=confidence, importance=importance, status="active", valid_from=now,
                       last_confirmed=now, source_episode_id=episode_id, embedding=vec,
                       embedding_model=settings.embedding_model if vec else None, expires_at=_expires())
    db.add(new)
    db.flush()
    if active is not None:
        active.status, active.valid_to = "superseded", now
        _link(db, cp, ("fact", new.id), ("fact", active.id), "supersedes")
    return action


# ── Slow path ────────────────────────────────────────────────────────────────

def summarise_episode(db: Session, episode_id: str, cp: ContactPoint, canned: dict | None = None) -> dict:
    """Fill in an episode's summary and upsert its facts. Consent re-checked at the last moment.
    `canned` (the Demo Console's seeded test customers only) replaces the model call with a fixed reply,
    which still goes through the same clean_episode checks and the same upsert path."""
    from app import contacts
    ep = db.query(ConversationEpisode).filter(ConversationEpisode.id == episode_id).first()
    if ep is None or not (settings.rag_enabled and contacts.has_full_memory_consent(cp)):
        return {}
    turns = db.query(VoiceTurn).filter(VoiceTurn.call_id == ep.call_id).order_by(VoiceTurn.turn_no).all()
    customer_lines = [t.text for t in turns if t.speaker == "customer"]
    conversation = "\n".join(f"{speaker_label(t.speaker)}: {t.text}" for t in turns)
    source = "llm"
    try:
        if canned is not None:
            source, out = "seed", clean_episode(canned)
        else:
            out = _llm_episode(conversation)
        if not out["summary"]:
            raise ValueError("empty summary")
    except Exception as e:
        write_audit_entry(db, "llm_failure_fallback", {"error": str(e)[:200], "stage": "memory_episode"})
        facts = clean_facts(rule_facts(customer_lines, ep.outcome))
        out = {"summary": extractive_summary(customer_lines, ep.outcome), "tone_arc": None, "key_events": [],
               "importance": rule_importance(ep.outcome, any(f["key"] == "hardship_flag" for f in facts)),
               "facts": facts}
        source = "rules"
    ep.summary, ep.tone_arc = out["summary"], out["tone_arc"]
    ep.key_events = json.dumps(out["key_events"]) if out["key_events"] else None
    ep.importance, ep.summarised_at, ep.summary_source = out["importance"], clock.now(), source
    actions = {"create": 0, "confirm": 0, "supersede": 0}
    for f in out["facts"]:
        actions[upsert_fact(db, cp, f["key"], f["value"], f["confidence"], out["importance"], ep.id)] += 1
    db.commit()
    write_audit_entry(db, "episode_summarised", {"contact_id": cp.id, "episode_id": ep.id, "source": source, **actions})
    return {"source": source, **actions}


def record_call(db: Session, call: VoiceCall, cp: ContactPoint) -> None:
    """Both steps, in order (called from memory.after_call on the background thread)."""
    ep = record_episode(db, call, cp)
    if ep is not None:
        summarise_episode(db, ep.id, cp)


def migrate_notes(db: Session, cp: ContactPoint) -> int:
    """A customer who agreed to the older "short notes" wording has now agreed to the current one: carry
    their notes over as facts and drop the old rows, so nothing they allowed is lost and nothing is read twice."""
    from app.rag_models import CustomerMemory
    rows = db.query(CustomerMemory).filter(CustomerMemory.contact_id == cp.id).order_by(CustomerMemory.created_at).all()
    moved = 0
    for r in rows:
        key = note_fact_key(r.kind, r.text)
        value = memory.scrub(r.text)
        if key and value:
            upsert_fact(db, cp, key, value, 0.6, 0.5, None)
            moved += 1
    for r in rows:
        db.delete(r)
    db.commit()
    if rows:
        write_audit_entry(db, "memory_notes_migrated", {"contact_id": cp.id, "notes": len(rows), "facts": moved})
    return moved

"""Small builders the DB suites share. Everything goes through the app's own models and functions; nothing leaves the machine."""
import itertools
import json
import uuid
from datetime import date, datetime, timedelta

_n = itertools.count(1)


def patch_world():
    """No network, no background threads, calls allowed inside the contact window. Safe to call repeatedly."""
    from app import clock, memory
    from app.scheduler import in_contact_window, next_contact_window_start
    memory.schedule_after_call = lambda call_id: None            # suites that test memory call the functions directly
    n = clock.now()
    if not in_contact_window(n):
        clock.advance_to(next_contact_window_start(n) + timedelta(seconds=1))


def contact(db, memory: str = "v2", verified: bool = True, voice: bool = True, language: str = "hinglish", phone: str | None = None):
    """A ContactPoint with the chosen consents. memory: v2 (current wording) | v1 (old 'short notes') | none."""
    from app import contacts
    from app.models import ContactPoint
    k = next(_n)
    cp = contacts.upsert_contact(db, razorpay_customer_id=f"cust_eval_{k}", phone=phone or f"+9198765{k:05d}"[:13], language=language)
    now = datetime.utcnow()
    cp.phone_verified_at = now if verified else None
    cp.voice_consent = voice
    cp.consent_at = now if voice else None
    cp.consent_source = "customer_ticked" if voice else None
    cp.memory_consent = memory in ("v1", "v2")
    cp.memory_consent_at = now if cp.memory_consent else None
    cp.memory_consent_version = contacts.MEMORY_CONSENT_TEXT_VERSION if memory == "v2" else None
    db.commit()
    db.refresh(cp)
    return cp


def call(db, cp, lines, routed: str | None = "callback_scheduled", days_ago: float = 0, subject_type: str = "invoice",
         subject_id: str | None = None, status: str = "completed", language: str = "hinglish"):
    """A finished call with its turns. lines = [("agent"|"customer", text), ...]."""
    from app.models import VoiceCall, VoiceTurn
    created = datetime.utcnow() - timedelta(days=days_ago)
    c = VoiceCall(subject_type=subject_type, subject_id=subject_id or f"INV-EVAL-{next(_n)}", customer_ref=cp.id, phone=cp.phone,
                  language=language, script_text=lines[0][1], status=status, routed_to=routed, created_at=created,
                  transcript="\n".join(f"{'Agent' if w == 'agent' else 'Customer'}: {t}" for w, t in lines))
    db.add(c)
    db.commit()
    for i, (who, text) in enumerate(lines):
        db.add(VoiceTurn(call_id=c.id, turn_no=i, speaker=who, text=text, created_at=created))
    db.commit()
    return c


def promise(db, cp, subject_id: str, status: str, days_ago_date: int = 3, amount: float = 1000, subject_type: str = "invoice"):
    from app.models import PaymentPromise
    p = PaymentPromise(subject_type=subject_type, subject_id=subject_id, customer_ref=cp.id, amount_inr=amount,
                       promised_date=date.today() - timedelta(days=days_ago_date), captured_via="voice", status=status,
                       created_at=datetime.utcnow() - timedelta(days=days_ago_date + 2))
    db.add(p)
    db.commit()
    return p


def episode(db, cp, lines, summary: dict | None = None, routed: str | None = None, days_ago: float = 0, subject_id: str | None = None):
    """Record a call into memory through the real write path (fast + slow), with a fixed summary instead of a model call."""
    from app import memory_write
    c = call(db, cp, lines, routed=routed, days_ago=days_ago, subject_id=subject_id)
    ep = memory_write.record_episode(db, c, cp)
    if ep is not None and summary is not None:
        memory_write.summarise_episode(db, ep.id, cp, canned=summary)
    return c, ep


def invoice_customer(db, memory: str = "v2", language: str = "hinglish", amount: float = 6500, verified: bool = True, voice: bool = True):
    """A business customer with one overdue invoice - what a real call is placed about. Returns (contact, invoice)."""
    from app import contacts, receivables, clock
    from app.models import BusinessAccount
    k = next(_n)
    phone = f"+9198765{k:05d}"[:13]
    acct = BusinessAccount(company_name=f"Eval Traders {k} [eval]", contact_name=f"Eval Person {k}", phone=phone, preferred_language=language,
                           voice_consent=voice)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    cp = contacts.upsert_contact(db, business_account_id=acct.id, phone=phone, language=language)
    now = datetime.utcnow()
    cp.phone_verified_at = now if verified else None
    cp.voice_consent = voice
    cp.consent_at = now if voice else None
    cp.consent_source = "customer_ticked" if voice else None
    cp.memory_consent = memory in ("v1", "v2")
    cp.memory_consent_at = now if cp.memory_consent else None
    cp.memory_consent_version = contacts.MEMORY_CONSENT_TEXT_VERSION if memory == "v2" else None
    db.commit()
    inv = receivables.create_invoice(db, acct, f"EVAL-{k:04d}", amount, issued_at=clock.now() - timedelta(days=39),
                                     due_at=clock.now() - timedelta(days=9))
    db.refresh(cp)
    return cp, inv


def counts(db, contact_id: str) -> dict:
    """Rows kept about one contact, per tier."""
    from app.rag_models import ConversationEpisode, CustomerFact, CustomerMemory, EpisodeChunk, MemoryLink
    q = lambda M: db.query(M).filter(M.contact_id == contact_id).count()
    return {"notes": q(CustomerMemory), "episodes": q(ConversationEpisode), "chunks": q(EpisodeChunk), "facts": q(CustomerFact), "links": q(MemoryLink)}


def no_model(memory_write):
    """Make the slow (model) step fail the way an outage does, so the rule fallback is what runs."""
    def boom(_conv):
        raise RuntimeError("model unavailable (eval)")
    memory_write._llm_episode = boom

"""Privacy: one customer's memory never reaches another; forgetting is complete; consent changes act at once."""
from datetime import datetime, timedelta

from evals import fixtures as fx
from evals.harness import Recorder


def run(ctx):
    from app import contacts, memory, memory_retrieval as mr
    from app.database import SessionLocal
    from app.rag_models import ConversationEpisode, CustomerFact, EpisodeChunk, MemoryLink
    fx.patch_world()
    R = Recorder("privacy", "No cross-customer leakage; forget removes every tier and its embeddings; consent withdrawal and a phone change delete; expiry sweeps.")
    db = SessionLocal()
    try:
        # two customers with deliberately near-identical memory
        a, b = fx.contact(db), fx.contact(db)
        shared = {"summary": "Customer said money arrives around the 5th and asked for evening calls.", "importance": 0.7,
                  "facts": [{"key": "salary_day", "value": "Money usually arrives around day 5 of the month."},
                            {"key": "preferred_call_time", "value": "Prefers to be called in the evening."}]}
        fx.episode(db, a, [("agent", "Namaste A"), ("customer", "A: meri salary 5 tareekh ko aati hai SECRET-A")], summary=shared)
        fx.episode(db, b, [("agent", "Namaste B"), ("customer", "B: meri salary 5 tareekh ko aati hai SECRET-B")], summary=shared)
        before_b = fx.counts(db, b.id)

        queries = ["pichli baar salary ke baare mein kya kaha tha", "last time what did I say about money", "meri salary kab aati hai", "evening call",
                   "SECRET-B", "SECRET-A", "5 tareekh", "kya baat hui thi", "payment", "namaste"]
        leaks = 0
        for who, other, secret in ((a, b, "SECRET-B"), (b, a, "SECRET-A")):
            for q in queries:
                m = mr.build(db, who, q, force_recall=True)
                text = " ".join(r["text"] for r in m.recalled) + " ".join(m.core)
                bad = secret in text
                leaks += bad
                R.case(f"no_leak:{secret}:{q[:25]}", not bad, f"customer's recall contained {secret}", critical=True)
        # also at the SQL level: every recalled episode belongs to the asker
        for who in (a, b):
            for r in mr.recall(db, who.id, "salary evening"):
                owner = db.query(ConversationEpisode).filter_by(id=r["episode_id"]).first().contact_id
                R.case(f"owner_check:{who.id[:6]}:{r['mid'] if 'mid' in r else r['kind']}", owner == who.id, "recalled an episode that belongs to someone else", critical=True)
        R.metric("cross_customer_leaks", leaks)
        R.require("cross_customer_leaks", "==", 0, "a leak between customers is the worst privacy failure")

        # forget removes everything, and only that customer
        n = memory.forget(db, a.id, reason="eval")
        left = fx.counts(db, a.id)
        R.case("forget:removes_every_tier", n > 0 and sum(left.values()) == 0, f"left: {left}", critical=True)
        R.case("forget:leaves_others_alone", fx.counts(db, b.id) == before_b, f"b before {before_b}, after {fx.counts(db, b.id)}", critical=True)
        R.case("forget:no_embeddings_left", db.query(EpisodeChunk).filter_by(contact_id=a.id).count() == 0 and db.query(CustomerFact).filter_by(contact_id=a.id).count() == 0,
               "vectors remain after forgetting")
        R.case("forget:no_dangling_links", db.query(MemoryLink).filter_by(contact_id=a.id).count() == 0, "")

        # consent withdrawal
        c = fx.contact(db)
        fx.episode(db, c, [("agent", "Namaste"), ("customer", "salary 5")], summary=shared)
        contacts.record_memory_consent(db, c, False, via="eval")
        R.case("withdrawal:deletes_everything", sum(fx.counts(db, c.id).values()) == 0, f"left: {fx.counts(db, c.id)}", critical=True)
        R.case("withdrawal:clears_consent_flags", not c.memory_consent and c.memory_consent_version is None, "")

        # phone change
        d = fx.contact(db)
        fx.episode(db, d, [("agent", "Namaste"), ("customer", "salary 5")], summary=shared)
        contacts.upsert_contact(db, razorpay_customer_id=d.razorpay_customer_id, phone="+919811122233")
        db.refresh(d)
        R.case("phone_change:deletes_everything", sum(fx.counts(db, d.id).values()) == 0, f"left: {fx.counts(db, d.id)}", critical=True)
        R.case("phone_change:clears_verification_and_consent", d.phone_verified_at is None and not d.memory_consent and not d.voice_consent, "")

        # expiry sweep
        e = fx.contact(db)
        _, ep = fx.episode(db, e, [("agent", "Namaste"), ("customer", "salary 5")], summary=shared)
        past = datetime.utcnow() - timedelta(days=1)
        db.query(ConversationEpisode).filter_by(id=ep.id).update({"expires_at": past})
        db.query(EpisodeChunk).filter_by(episode_id=ep.id).update({"expires_at": past})
        db.commit()
        memory.sweep_expired(db)
        left = fx.counts(db, e.id)
        R.case("sweep:expired_episode_and_chunks_gone", left["episodes"] == 0 and left["chunks"] == 0, f"left: {left}", critical=True)
        R.case("sweep:links_to_it_gone", left["links"] == 0 or db.query(MemoryLink).filter_by(contact_id=e.id, src_type="episode", src_id=ep.id).count() == 0, f"left: {left}")
        R.case("sweep:unexpired_facts_kept", left["facts"] >= 1, f"left: {left}")
        return R.result
    finally:
        db.close()

"""Memory write path: verbatim storage, consent gating, facts, conflict resolution, links, fallback, migration."""
from evals import fixtures as fx
from evals.harness import Recorder


def run(ctx):
    from app import memory, memory_write as mw
    from app.database import SessionLocal
    from app.rag_models import ConversationEpisode, CustomerFact, CustomerMemory, EpisodeChunk, MemoryLink
    fx.patch_world()
    def _offline(*a, **k):
        raise RuntimeError("model unavailable (eval)")
    memory._llm_notes = _offline     # the old notes path must not reach the real model in an offline run
    fx.no_model(mw)                  # deterministic: the model step is replaced by canned summaries or by the rule fallback
    R = Recorder("memory_write", "Calls are stored word for word, only with the current consent; facts are clean, allow-listed and superseded not overwritten; failures fall back audibly.")
    db = SessionLocal()
    try:
        # 1. verbatim: every turn of calls of many lengths reappears, byte for byte and in order, in the stored windows
        exact = total = 0
        for n_turns in (1, 2, 3, 4, 5, 6, 9, 12):
            cp = fx.contact(db)
            lines = [("agent" if i % 2 == 0 else "customer", f'turn {i}: "quotes", ₹{i * 100}, तारीख {i}, 9876543210 and trailing space ') for i in range(n_turns)]
            c, ep = fx.episode(db, cp, lines)
            chunks = db.query(EpisodeChunk).filter(EpisodeChunk.episode_id == ep.id).order_by(EpisodeChunk.seq).all()
            blob = "\n".join(k.text for k in chunks)
            ok = all(f"{'Agent' if w == 'agent' else 'Customer'}: {t}" in blob for w, t in lines)
            seen = sorted({t for k in chunks for t in range(k.turn_from, k.turn_to + 1)})
            ok = ok and seen == list(range(n_turns)) and all(k.embedding is not None for k in chunks)
            total += 1
            exact += ok
            R.case(f"verbatim_{n_turns}_turns", ok, f"windows cover {seen}; text intact={all(t in blob for _, t in lines)}", critical=True)
        R.rate("verbatim_exact_rate", exact, total)
        R.require("verbatim_exact_rate", "==", 1.0, "what was said must be stored exactly as said")

        # 2. consent gating: only the current wording unlocks tiered memory
        for label, mem in (("no_consent", "none"), ("old_wording_v1", "v1")):
            cp = fx.contact(db, memory=mem)
            c = fx.call(db, cp, [("agent", "Namaste"), ("customer", "meri salary 5 tareekh ko aati hai")])
            R.case(f"{label}:record_episode_returns_none", mw.record_episode(db, c, cp) is None, "an episode was recorded without the current consent", critical=True)
            memory.after_call(c.id) if mem == "v1" else None
            k = fx.counts(db, cp.id)
            R.case(f"{label}:no_tiered_rows", k["episodes"] == k["chunks"] == k["facts"] == k["links"] == 0, f"rows kept: {k}", critical=True)
            if mem == "v1":
                R.case("old_wording_v1:short_notes_still_kept", k["notes"] >= 1, f"notes kept: {k['notes']} (the old behaviour must continue)")
            else:
                R.case("no_consent:nothing_at_all_kept", k["notes"] == 0, f"notes kept: {k['notes']}", critical=True)

        # 3. idempotent
        cp = fx.contact(db)
        c, ep = fx.episode(db, cp, [("agent", "Namaste"), ("customer", "ok")])
        again = mw.record_episode(db, c, cp)
        R.case("record_is_idempotent", again is not None and again.id == ep.id and db.query(ConversationEpisode).filter_by(call_id=c.id).count() == 1,
               "a second call to record_episode made a second episode", critical=True)

        # 4. rule fallback when the model is down - audibly
        cp = fx.contact(db)
        c, ep = fx.episode(db, cp, [("agent", "Namaste"), ("customer", "meri salary 5 tareekh ko aati hai, shaam ko call karna")], routed="callback_scheduled")
        out = mw.summarise_episode(db, ep.id, cp)
        db.refresh(ep)
        keys = {f.key for f in db.query(CustomerFact).filter_by(contact_id=cp.id)}
        R.case("fallback:source_is_rules", out.get("source") == "rules" and ep.summary_source == "rules", f"source={out}")
        R.case("fallback:facts_extracted", {"salary_day", "preferred_call_time"} <= keys, f"facts: {keys}")
        from app.models import AuditLog
        R.case("fallback:audited", db.query(AuditLog).filter(AuditLog.action_type == "llm_failure_fallback", AuditLog.details.like("%memory_episode%")).count() >= 1,
               "no llm_failure_fallback audit entry", critical=True)

        # 5. facts: allow-listed keys, scrubbed values, nothing sensitive, hardship as a flag only
        cp = fx.contact(db)
        canned = {"summary": "Customer shared a phone number 9876543210 and mentioned the family hospital visit.", "importance": 0.6, "tone_arc": "tense",
                  "key_events": ["called 9876543210", "hospital visit"],
                  "facts": [{"key": "salary_day", "value": "around day 5, call 9876543210", "confidence": 0.9},
                            {"key": "favourite_colour", "value": "blue"},
                            {"key": "preference", "value": "mother is in hospital"},
                            {"key": "hardship_flag", "value": "Mentioned a money difficulty recently; be gentle."}]}
        c, ep = fx.episode(db, cp, [("agent", "Namaste"), ("customer", "mother is in hospital, call 9876543210")], summary=canned)
        facts = db.query(CustomerFact).filter_by(contact_id=cp.id).all()
        blob = " ".join([ep.summary or "", ep.key_events or ""] + [f.value for f in facts])
        R.case("clean:no_phone_number_stored", "9876543210" not in blob, "a phone number reached the summary or facts", critical=True)
        R.case("clean:disallowed_key_dropped", all(f.key != "favourite_colour" for f in facts), "a key outside the allow-list was stored", critical=True)
        R.case("clean:sensitive_fact_dropped", all("hospital" not in f.value.lower() for f in facts), "a sensitive fact was stored", critical=True)
        R.case("clean:hardship_is_a_flag", any(f.key == "hardship_flag" for f in facts), "the hardship flag was lost")
        R.case("clean:all_keys_allowed", all(f.key in mw.FACT_KEYS for f in facts), "", critical=True)
        # rule summary never quotes hardship details
        s = mw.extractive_summary(["meri job chali gayi hai", "salary 5 tareekh ko aati hai"], None)
        R.case("clean:rule_summary_hides_hardship_details", "job" not in s.lower(), f"summary: {s}", critical=True)

        # 6. conflict resolution
        cp = fx.contact(db)
        sal = lambda v: {"summary": "Call.", "importance": 0.5, "facts": [{"key": "salary_day", "value": v}]}
        fx.episode(db, cp, [("agent", "a"), ("customer", "salary 5")], summary=sal("Money arrives around day 5."), days_ago=3)
        fx.episode(db, cp, [("agent", "a"), ("customer", "salary 5 again")], summary=sal("Money arrives around day 5."), days_ago=2)
        rows = db.query(CustomerFact).filter_by(contact_id=cp.id, key="salary_day").all()
        R.case("conflict:same_value_confirms_not_duplicates", len(rows) == 1 and rows[0].status == "active", f"{len(rows)} rows")
        fx.episode(db, cp, [("agent", "a"), ("customer", "salary 10 now")], summary=sal("Money arrives around day 10."), days_ago=1)
        rows = db.query(CustomerFact).filter_by(contact_id=cp.id, key="salary_day").order_by(CustomerFact.valid_from).all()
        active = [r for r in rows if r.status == "active"]
        old = [r for r in rows if r.status == "superseded"]
        R.case("conflict:one_active_value", len(active) == 1 and "day 10" in active[0].value, f"active: {[r.value for r in active]}", critical=True)
        R.case("conflict:old_value_kept_with_end_date", len(old) == 1 and old[0].valid_to is not None, f"superseded: {len(old)}")
        link = db.query(MemoryLink).filter_by(contact_id=cp.id, relation="supersedes").count()
        R.case("conflict:supersedes_link", link == 1, f"{link} supersedes links")

        # 7. links derived from records
        cp = fx.contact(db)
        subj = "INV-LINKS-1"
        c1, e1 = fx.episode(db, cp, [("agent", "a"), ("customer", "10 tareekh tak kar dunga")], routed="promise_created:2026-10-10", days_ago=5, subject_id=subj)
        fx.promise(db, cp, subj, "broken", days_ago_date=2)
        c2, e2 = fx.episode(db, cp, [("agent", "a"), ("customer", "sorry")], routed="callback_scheduled", days_ago=0, subject_id=subj)
        rel = {(l.relation, l.src_type, l.dst_type) for l in db.query(MemoryLink).filter_by(contact_id=cp.id)}
        R.case("links:temporal_next", ("temporal_next", "episode", "episode") in rel, f"{rel}")
        R.case("links:entity_to_debt", ("entity", "episode", "subject") in rel, f"{rel}")
        R.case("links:causal_after_broken_promise", ("causal", "episode", "episode") in rel, f"{rel}")

        # 8. migration of the old notes
        cp = fx.contact(db, memory="v1")
        memory.write_notes(db, cp, "eval", [{"kind": "callback_time", "text": "Prefers to be called in the evening."},
                                            {"kind": "stated_constraint", "text": "Said money usually arrives around day 10 of the month."}])
        before = db.query(CustomerMemory).filter_by(contact_id=cp.id).count()
        from app import contacts
        contacts.record_memory_consent(db, cp, True, via="eval")
        keys = {f.key for f in db.query(CustomerFact).filter_by(contact_id=cp.id, status="active")}
        R.case("migrate:notes_become_facts", before == 2 and {"preferred_call_time", "salary_day"} <= keys, f"notes before={before}, facts={keys}", critical=True)
        R.case("migrate:old_notes_removed", db.query(CustomerMemory).filter_by(contact_id=cp.id).count() == 0, "old notes were left behind")
        return R.result
    finally:
        db.close()

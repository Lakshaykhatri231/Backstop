"""The Demo Console's test customers: each is in exactly the state it claims, and Reset puts it back."""
from evals import fixtures as fx
from evals.harness import Recorder

# what each seeded customer must look like: (can_call, block_reason, episodes, facts>=, notes, memory_consent, current_wording)
EXPECT = {
    "asha":   dict(can_call=True,  block=None,                  episodes=0, facts_min=0, notes=0, memory=True,  current=True),
    "rohit":  dict(can_call=True,  block=None,                  episodes=2, facts_min=3, notes=0, memory=True,  current=True),
    "sunita": dict(can_call=True,  block=None,                  episodes=1, facts_min=2, notes=0, memory=True,  current=True),
    "vikram": dict(can_call=True,  block=None,                  episodes=0, facts_min=0, notes=2, memory=True,  current=False),
    "neha":   dict(can_call=True,  block=None,                  episodes=0, facts_min=0, notes=0, memory=False, current=False),
    "imran":  dict(can_call=False, block="phone_not_verified",  episodes=0, facts_min=0, notes=0, memory=False, current=False),
    "karan":  dict(can_call=True,  block=None,                  episodes=0, facts_min=0, notes=0, memory=True,  current=True),
}


def run(ctx):
    from app import demo_personas as dp, memory_retrieval as mr
    from app.database import SessionLocal
    from app.models import ContactPoint, PaymentPromise, VoiceCall
    fx.patch_world()
    R = Recorder("personas", "Every test customer is in the state its description promises; Reset restores it; their history is usable by the real memory read path.")
    db = SessionLocal()
    try:
        out = {p["key"]: p for p in dp.seed_all(db)}
        R.case("all_seven_created", set(out) == set(EXPECT), f"created: {sorted(out)}", critical=True)
        for key, exp in EXPECT.items():
            p = out.get(key)
            if not p:
                continue
            c, t = p["contact"], p["tiers"]
            R.case(f"{key}:call_permission", c["can_call"] == exp["can_call"] and c["block_reason"] == exp["block"], f"can_call={c['can_call']} block={c['block_reason']}", critical=True)
            R.case(f"{key}:episodes", t["episodes"] == exp["episodes"], f"episodes={t['episodes']}, expected {exp['episodes']}", critical=True)
            R.case(f"{key}:facts", t["facts"] >= exp["facts_min"], f"facts={t['facts']}, expected at least {exp['facts_min']}")
            R.case(f"{key}:old_notes", t["notes"] == exp["notes"], f"notes={t['notes']}, expected {exp['notes']}", critical=True)
            R.case(f"{key}:memory_consent_state", bool(c.get("memory_consent")) == exp["memory"], f"memory_consent={c.get('memory_consent')}")
            R.case(f"{key}:has_a_debt_to_call_about", bool(p.get("subject_id")) and p.get("subject_type") in ("invoice", "mandate_run"), "", critical=True)
            R.case(f"{key}:has_test_script", len(p["tests"]) >= 2 and all(x["say"] and x["expect"] for x in p["tests"]), "")
        # the seeded consent is labelled as seed data, never passed off as customer-ticked in the audit sense
        labelled = db.query(ContactPoint).filter(ContactPoint.consent_text_version == dp.CONSENT_LABEL).count()
        R.case("seeded_consent_is_labelled", labelled >= 5, f"{labelled} contacts labelled {dp.CONSENT_LABEL!r}")

        # the seeded history works with the real memory read path
        cp = db.query(ContactPoint).filter(ContactPoint.id == out["rohit"]["contact_id"]).first()
        m = mr.build(db, cp, "pichli baar maine kya kaha tha")
        text = " ".join(r["text"] for r in m.recalled) + " ".join(m.core)
        R.case("rohit:history_is_recallable", "5" in text and "evening" in text.lower(), f"recalled: {text[:120]!r}", critical=True)
        cp = db.query(ContactPoint).filter(ContactPoint.id == out["sunita"]["contact_id"]).first()
        R.case("sunita:gentle_handling_flag_in_profile", any("gentle" in c.lower() for c in mr.build(db, cp, "kitna bakaya").core), "", critical=True)
        broken = db.query(PaymentPromise).filter_by(subject_id=out["sunita"]["subject_id"], status="broken").count()
        R.case("sunita:has_a_broken_promise_on_her_debt", broken == 1, f"{broken} broken promises")
        cp = db.query(ContactPoint).filter(ContactPoint.id == out["vikram"]["contact_id"]).first()
        R.case("vikram:old_wording_gets_no_recall", mr.build(db, cp, "pichli baar").empty(), "memory returned for the old consent wording", critical=True)

        # idempotent seeding; reset restores
        again = {p["key"]: p for p in dp.seed_all(db)}
        R.case("seed_is_idempotent", all(again[k]["subject_id"] == out[k]["subject_id"] for k in out), "seeding twice changed a debt")
        # dirty a customer, then reset
        victim = out["rohit"]
        from app import memory
        memory.forget(db, victim["contact_id"], reason="eval-dirty")
        fx.call(db, db.query(ContactPoint).filter_by(id=victim["contact_id"]).first(), [("agent", "x")], subject_id=victim["subject_id"])
        back = dp.reset_persona(db, "rohit")
        R.case("reset:history_restored", back["tiers"]["episodes"] == 2 and back["tiers"]["facts"] >= 3, f"tiers: {back['tiers']}", critical=True)
        R.case("reset:extra_calls_removed", back["calls"] == 2, f"calls: {back['calls']}, expected the 2 seeded", critical=True)
        R.case("reset:keeps_the_same_debt", back["subject_id"] == victim["subject_id"], "a reset replaced a debt that was still open")
        R.case("reset:can_be_called_again", back["contact"]["can_call"], "")
        return R.result
    finally:
        db.close()

"""The agent approaches each customer differently, and sounds like a person doing it.

Covers app/customer_profile.py end to end: the history -> approach rules, the usual-pay-day offer, call timing, consent gating, and the
"human" properties (fillers rationed and varied, a returning customer not hearing the same ones, calm after someone was upset). No model
is called: the turn model is replaced by a stub, and a short reply never needed one."""
import re
from datetime import datetime, timedelta

from evals import fixtures as fx
from evals.harness import Recorder
from evals.suites.flow import Flow

_IST = timedelta(hours=5, minutes=30)
HARDSHIP_WORDS = ("mushkil", "pareshani", "difficulty", "hardship", "money trouble", "dikkat")


def _canned(facts, tone="calm -> calm", summary="Customer spoke with us."):
    return {"summary": summary, "tone_arc": tone, "key_events": ["Spoke with us"], "importance": 0.6,
            "facts": [{"key": k, "value": v, "confidence": 0.8} for k, v in facts]}


def run(ctx):
    from app import clock, contacts, customer_profile as cpf, knowledge, persona, speech, voice, voice_chat as vc
    from app.database import SessionLocal
    from app.models import AuditLog, PaymentPromise, ScheduledAction
    fx.patch_world()
    R = Recorder("personalisation", "History changes how the agent approaches a customer (approach, usual date, call time) - only with consent, "
                                    "never by speaking private details - and the result sounds human: fillers rationed and varied, calm after distress.")
    db = SessionLocal()
    f = Flow(db, vc, voice, clock)
    original_llm = vc._llm_turn
    try:
        # ── 1. the pure rules ───────────────────────────────────────────────
        for text, want in [("Money usually arrives around day 5 of the month.", 5), ("paise 12 tareekh ko aate hain", 12),
                           ("salary on the 25th", 25), ("Prefers evenings", None), ("around day 45", None), ("", None)]:
            R.case(f"pay_day:{text[:30]!r}", cpf.parse_pay_day(text) == want, f"read {cpf.parse_pay_day(text)}, expected {want}")
        for text, want in [("Prefers to be called in the evening.", "evening"), ("shaam ko call karna", "evening"), ("subah 10 baje", "morning"),
                           ("dopahar mein", "afternoon"), ("whenever", None)]:
            R.case(f"slot:{text[:30]!r}", cpf.slot_from_text(text) == want, f"read {cpf.slot_from_text(text)}, expected {want}")
        D = lambda facts=None, kept=0, broken=0, partial=False, disputed=False, tone=None, consented=True: cpf.derive(
            facts or {}, kept, broken, partial, disputed, tone, consented)
        approach_cases = [
            ("hardship_fact_is_gentle", D({"hardship_flag": "be gentle"}).approach, "gentle"),
            ("hardship_beats_a_broken_promise", D({"hardship_flag": "x"}, broken=2).approach, "gentle"),
            ("worried_last_time_is_gentle", D(tone="worried -> worried").approach, "gentle"),
            ("calmed_down_is_not_gentle", D(tone="tense -> relieved").approach, "standard"),
            ("disputed_before_explains_first", D(disputed=True).approach, "explain_first"),
            ("broken_only_is_steady", D(broken=1).approach, "steady"),
            ("kept_only_is_brief", D(kept=2).approach, "brief"),
            ("kept_and_broken_is_standard", D(kept=1, broken=1).approach, "standard"),
            ("no_history_is_standard", D().approach, "standard"),
            ("facts_ignored_without_consent", D({"hardship_flag": "x", "salary_day": "day 5"}, consented=False).approach, "standard"),
        ]
        for id_, got, want in approach_cases:
            R.case(f"approach:{id_}", got == want, f"got {got}, expected {want}", critical=True)
        R.case("facts_without_consent_leave_no_pay_day", D({"salary_day": "day 5"}, consented=False).pay_day is None, "a fact was used without consent", critical=True)

        from datetime import date
        S = cpf.suggested_date
        R.case("suggest:next_occurrence", S(5, date(2026, 10, 10), 30) == date(2026, 11, 5), str(S(5, date(2026, 10, 10), 30)))
        R.case("suggest:today_is_not_the_future", S(10, date(2026, 10, 10), 40) == date(2026, 11, 10), str(S(10, date(2026, 10, 10), 40)))
        R.case("suggest:outside_the_horizon_is_nothing", S(5, date(2026, 10, 10), 7) is None, "a date past the promise limit was offered", critical=True)
        R.case("suggest:day_the_month_lacks", S(31, date(2026, 6, 10), 30) == date(2026, 6, 30), str(S(31, date(2026, 6, 10), 30)))
        R.case("suggest:no_pay_day_no_date", S(None, date(2026, 10, 10), 30) is None, "")

        base = datetime(2026, 10, 24, 4, 0)             # 09:30 IST
        for slot in ("morning", "afternoon", "evening"):
            moved = cpf.shift_to_slot(base, slot, earliest=base - timedelta(hours=1), latest=base + timedelta(days=1, hours=12))
            ist = moved + _IST
            R.case(f"shift:{slot}_stays_in_the_calling_window", 8 <= ist.hour < 19 and (ist.hour, ist.minute) == cpf.SLOT_HOUR[slot], f"moved to {ist}", critical=True)
        R.case("shift:never_outside_the_bounds", cpf.shift_to_slot(base, "evening", earliest=base + timedelta(hours=1), latest=base + timedelta(hours=2)) == base,
               "moved a call outside earliest/latest", critical=True)
        R.case("shift:no_slot_no_change", cpf.shift_to_slot(base, None, base, base + timedelta(days=2)) == base, "")

        # ── 2. a reliable customer who told us when money arrives ───────────
        today = f.today()
        pay_day = (today + timedelta(days=10)).day
        usual = today + timedelta(days=10)
        cp, inv = fx.invoice_customer(db)
        fx.episode(db, cp, [("agent", "Namaste! Kab tak payment ho payega?"), ("customer", "paise mahine ki tareekh ko aate hain, shaam ko call karna")],
                   summary=_canned([("salary_day", f"Money usually arrives around day {pay_day} of the month."),
                                    ("preferred_call_time", "Prefers to be called in the evening.")], tone="tense -> relieved"), days_ago=2)
        fx.promise(db, cp, inv.id, "kept")
        started = f.start(inv)
        c = started["call_id"]
        call = db.query(voice.VoiceCall).filter_by(id=c).first()
        prof = cpf.CustomerProfile.from_json(call.profile_json)
        R.case("profile:stored_on_the_call_at_its_start", prof is not None and prof.approach == "brief" and prof.pay_day == pay_day and prof.call_slot == "evening",
               f"profile={call.profile_json}", critical=True)
        script = started["script"]
        thanks = cpf.THANKS["hinglish"]
        R.case("opening:thanks_a_customer_who_kept_their_promise", thanks in script and script.index("record ho sakti hai.") < script.index(thanks) < script.index("Kab tak"),
               f"script: {script[:220]!r}", critical=True)
        R.case("opening:disclosure_still_first", script.index("AI assistant") < script.index(thanks), script[:120])

        out, kind, text = f.say(c, "haan", f.down())
        R.case("short_haan:offers_the_usual_date", kind == "confirm_date" and usual.strftime("%d %b") in text and not out.get("done"),
               f"kind={kind} text={text!r}: a bare 'haan' did not use what we remember", critical=True)
        out, kind, text = f.say(c, "haan", f.down())
        booked = db.query(PaymentPromise).filter_by(subject_type="invoice", subject_id=inv.id, status="open").all()
        R.case("short_haan:booking_the_offered_date", out.get("done") and len(booked) == 1 and booked[0].promised_date == usual,
               f"done={out.get('done')} routed={out.get('routed_to')} promises={[(p.promised_date, p.status) for p in booked]}", critical=True)

        # the same through the model-down fallback with a longer sentence
        cp2, inv2 = fx.invoice_customer(db)
        fx.episode(db, cp2, [("agent", "Namaste!"), ("customer", "paise aate hain")], summary=_canned([("salary_day", f"Money usually arrives around day {pay_day} of the month.")]), days_ago=2)
        c2 = f.start(inv2)["call_id"]
        out, kind, text = f.say(c2, "haan main pay kar dunga", f.down())
        R.case("fallback:keyword_reader_also_offers_the_usual_date", kind == "confirm_date" and usual.strftime("%d %b") in text, f"kind={kind} text={text!r}", critical=True)
        out, kind, text = f.say(c2, "nahi", f.down())
        R.case("usual_date:a_no_does_not_loop", kind != "confirm_date", f"kind={kind}: the same date was offered twice")

        # ── 3. consent: the same facts, but the customer no longer allows memory ─
        cp3, inv3 = fx.invoice_customer(db)
        fx.episode(db, cp3, [("agent", "Namaste!"), ("customer", "paise aate hain")], summary=_canned([("salary_day", f"Money usually arrives around day {pay_day} of the month."),
                   ("hardship_flag", "Mentioned a money difficulty recently; be gentle.")]), days_ago=2)
        cp3.memory_consent = False
        db.commit()
        c3 = f.start(inv3)["call_id"]
        prof3 = cpf.CustomerProfile.from_json(db.query(voice.VoiceCall).filter_by(id=c3).first().profile_json)
        R.case("consent:facts_not_used_without_it", prof3 is not None and prof3.pay_day is None and prof3.approach == "standard" and not prof3.consented,
               f"profile={prof3}", critical=True)
        out, kind, text = f.say(c3, "haan", f.down())
        R.case("consent:no_usual_date_offered", kind == "ask_date", f"kind={kind}: used a remembered fact without consent", critical=True)

        # ── 4. someone who has had a hard time: gentler words, never spoken about ─
        cp4, inv4 = fx.invoice_customer(db)
        fx.episode(db, cp4, [("agent", "Namaste!"), ("customer", "paise ki dikkat hai")],
                   summary=_canned([("hardship_flag", "Mentioned a money difficulty recently; be gentle.")], tone="worried -> hopeful"), days_ago=2)
        s4 = f.start(inv4)
        c4 = s4["call_id"]
        prof4 = cpf.CustomerProfile.from_json(db.query(voice.VoiceCall).filter_by(id=c4).first().profile_json)
        R.case("gentle:approach_chosen", prof4 is not None and prof4.approach == "gentle", f"{prof4}", critical=True)
        out, kind, text = f.say(c4, "haan", f.down())
        R.case("gentle:softer_ask_for_a_date", kind == "ask_date" and text.startswith("Koi jaldi nahi"), f"kind={kind} text={text!r}")
        spoken = (s4["script"] + " " + text).lower()
        R.case("gentle:the_difficulty_is_never_spoken", not any(w in spoken for w in HARDSHIP_WORDS), f"spoken: {spoken[:200]!r}", critical=True)
        R.case("gentle:warmer_voice_style", persona.style_for("Theek hai.", "answer", "gentle")[0] == "empathetic", "")
        audit = db.query(AuditLog).filter(AuditLog.action_type == "voice_profile_built").all()
        leaked = [a for a in audit if any(w in (a.details or "").lower() for w in HARDSHIP_WORDS)]
        R.case("audit:profile_entries_hold_no_personal_text", bool(audit) and not leaked, f"{len(audit)} entries, {len(leaked)} with personal wording", critical=True)

        # someone who paid in parts before is offered that again
        cp5, inv5 = fx.invoice_customer(db)
        fx.promise(db, cp5, inv5.id, "partially_kept")
        c5 = f.start(inv5)["call_id"]
        out, kind, text = f.say(c5, "haan", f.down())
        R.case("partial:part_payment_offered_again", kind == "ask_date" and persona.PART_PAYMENT_HINT["hinglish"] in text, f"kind={kind} text={text!r}")

        # ── 5. calm after someone was upset ─────────────────────────────────
        class T:                                    # a stored turn, as far as effective_approach looks
            def __init__(self, speaker, kind):
                self.speaker, self.kind = speaker, kind
        R.case("carry_over:stays_gentle_after_hardship", vc.effective_approach("brief", [T("agent", "script"), T("agent", "hardship")]) == "gentle", "went back to brisk", critical=True)
        R.case("carry_over:stays_gentle_after_an_apology", vc.effective_approach("standard", [T("agent", "apology")]) == "gentle", "")
        R.case("carry_over:untouched_otherwise", vc.effective_approach("brief", [T("agent", "script"), T("agent", "answer")]) == "brief", "")

        # ── 6. fillers: rationed by approach, never twice in a row, varied across calls ─
        QS = [("Q_AMOUNT", "yeh amount kitna hai batao"), ("Q_WHY", "kis cheez ka hai yeh sab"), ("Q_DUE", "kab tak due tha yeh wala"),
              ("Q_HOW_PAY", "kaise karna hai payment mujhe"), ("Q_WHO", "aap kaun bol rahe ho yahan")]

        def five_turns(inv):
            cid = f.start(inv)["call_id"]
            for intent, line in QS:
                f.say(cid, line, f.llm(intent))
            rows = vc._turns(db, cid)
            return [t.text for t in rows if t.speaker == "agent" and t.kind not in ("script",)]

        def fillers(lines):
            return [persona.starts_with_filler(t) for t in lines]

        cps, invs = fx.invoice_customer(db)                          # standard
        std = five_turns(invs)
        cpb, invb = fx.invoice_customer(db)                          # brief: kept promise
        fx.promise(db, cpb, invb.id, "kept")
        brief = five_turns(invb)
        cpg, invg = fx.invoice_customer(db)                          # gentle
        fx.episode(db, cpg, [("agent", "Namaste!"), ("customer", "paise ki dikkat hai")], summary=_canned([("hardship_flag", "gentle")], tone="worried -> hopeful"), days_ago=2)
        gentle = five_turns(invg)
        for name, lines, lo, hi in (("standard", std, 0, 2), ("brief", brief, 0, 1), ("gentle", gentle, 1, 3)):
            n = sum(fillers(lines))
            R.metric(f"{name}_fillers_in_5_replies", n)
            R.case(f"fillers:{name}_rate_in_band", lo <= n <= hi, f"{n} of {len(lines)} replies start with a filler (expected {lo}-{hi}): {[t[:18] for t in lines]}", critical=True)
            R.case(f"fillers:{name}_never_two_in_a_row", not any(a and b for a, b in zip(fillers(lines), fillers(lines)[1:])), f"{[t[:18] for t in lines]}")
            leads = [persona.lead(t) for t in lines]
            R.case(f"variety:{name}_no_line_opens_like_the_one_before", all(a != b for a, b in zip(leads, leads[1:])), f"leads: {leads}")
        R.case("fillers:gentle_uses_softer_openers", any(persona.lead(t) in {persona.lead(x) for x in persona.GENTLE_ACK["hinglish"] + persona.EMPATHY["hinglish"]}
                                                          for t in gentle if persona.starts_with_filler(t)), f"{[t[:24] for t in gentle]}")
        R.case("fillers:the_customers_name_is_not_repeated", all(vc_name not in " ".join(std[1:]) for vc_name in ("Eval Person",)), "a name crept into replies")

        # a returning customer does not hear the same fillers in the same way
        cpr, invr = fx.invoice_customer(db)
        fx.call(db, cpr, [("agent", "Namaste! Kab tak payment ho payega?"), ("customer", "dekhte hain"), ("agent", "Acha, theek hai. Aap kab tak kar paayenge?"),
                          ("customer", "pata nahi"), ("agent", "Ji, samajh gayi. Koi tareekh?")], routed="callback_scheduled", days_ago=2)
        again = five_turns(invr)
        reused = [t[:14] for t in again if persona.lead(t) in ("acha", "ji")]
        R.case("variety:no_déjà_vu_from_the_last_call", not reused, f"reused the last call's fillers: {reused}")

        # ── 7. call timing ──────────────────────────────────────────────────
        def due_hours(inv_id):
            rows = db.query(ScheduledAction).filter_by(kind="voice_call", subject_id=inv_id).all()
            return [(r.due_at + _IST) for r in rows]

        cpt, invt = fx.invoice_customer(db)
        fx.episode(db, cpt, [("agent", "Namaste!"), ("customer", "shaam ko call karna")], summary=_canned([("preferred_call_time", "Prefers to be called in the evening.")]), days_ago=2)
        ct = f.start(invt)["call_id"]
        f.say(ct, "baad mein karna", f.down())
        when = due_hours(invt.id)
        R.case("timing:remembered_evening_for_a_callback", len(when) == 1 and (when[0].hour, when[0].minute) == (17, 0), f"scheduled at {when}", critical=True)
        cpw, invw = fx.invoice_customer(db)
        cw = f.start(invw)["call_id"]
        f.say(cw, "abhi busy hoon, shaam ko call karna", f.llm("CALLBACK"))
        when = due_hours(invw.id)
        R.case("timing:the_time_they_asked_for_wins", len(when) == 1 and when[0].hour == 17, f"scheduled at {when}", critical=True)
        cpn, invn = fx.invoice_customer(db)
        cn = f.start(invn)["call_id"]
        f.say(cn, "baad mein karna", f.down())
        when = due_hours(invn.id)
        R.case("timing:default_stays_10am", len(when) == 1 and (when[0].hour, when[0].minute) == (10, 0), f"scheduled at {when}")

        # ── 8. the voice: standard voices change pace per approach; the cache knows ─
        rate = lambda ssml: int(re.search(r"rate='([+-]\d+)%'", ssml).group(1))
        line = "Theek hai. Kab tak payment ho payega?"
        R.case("voice:gentle_is_slower_than_normal", rate(speech.delivery_ssml(line, "gentle")) < rate(speech.delivery_ssml(line)), "")
        R.case("voice:brief_is_quicker_than_normal", rate(speech.delivery_ssml(line, "brief")) > rate(speech.delivery_ssml(line)), "")
        R.case("voice:cache_separates_approaches", speech._cache_key("v", None, None, line, "gentle") != speech._cache_key("v", None, None, line, None), "a gentle clip would be reused for everyone")

        # ── 8b. Dragon HD (the default voice): only pace and pauses are obeyed, so only those are sent ──
        import httpx
        from app.config import settings
        line2 = "Main samajh sakti hoon... aisa ho jata hai. Aap bas ek date bata dijiye."
        gen, brf, std = speech.hd_delivery_ssml(line2, "gentle"), speech.hd_delivery_ssml(line2, "brief"), speech.hd_delivery_ssml(line2, "standard")
        R.case("hd:standard_stays_plain_text", std == speech.escape(line2) and speech.hd_delivery_ssml(line2, None) == std, "the plain HD voice was changed")
        R.case("hd:gentle_is_slower_with_pauses", "rate='-3%'" in gen and "<break" in gen, gen[:160], critical=True)
        R.case("hd:brief_is_quicker_without_extra_pauses", "rate='+6%'" in brf and "<break" not in brf, brf[:160])
        R.case("hd:no_pitch_or_style_is_sent", not any(w in gen + brf for w in ("pitch=", "express-as")), "sent something the voice ignores")
        old_flag = settings.azure_hd_delivery
        settings.azure_hd_delivery = False
        try:
            R.case("hd:switch_off_gives_plain_text", speech.hd_delivery_ssml(line2, "gentle") == speech.escape(line2), "")
        finally:
            settings.azure_hd_delivery = old_flag
        if speech.is_hd(speech._voice_for("hinglish")[0]):
            sent = {}

            class _Reply:
                status_code, content = 200, b"mp3"

            def _fake_post(url, headers=None, content=None, timeout=None):
                sent["ssml"] = content.decode()
                return _Reply()
            saved = (speech.tts_provider, httpx.post, settings.azure_speech_region)
            speech.tts_provider, httpx.post, settings.azure_speech_region = (lambda: "azure"), _fake_post, "centralindia"
            try:
                speech.synthesize(line2, "hinglish", None, None, approach="gentle")
                with_approach = sent["ssml"]
                speech.synthesize(line2, "hinglish", None, None)
                without = sent["ssml"]
            finally:
                speech.tts_provider, httpx.post, settings.azure_speech_region = saved
            R.case("hd:the_request_carries_the_approach", "rate='-3%'" in with_approach and "<prosody" not in without,
                   f"with: {with_approach[-160:]!r} without: {without[-120:]!r}", critical=True)

        # ── 9. the Demo Console's test customers come out as their descriptions say ─
        from app import demo, demo_personas as dp
        seeded = {x["key"]: x for x in dp.seed_all(db)}
        expect = {"rohit": ("brief", 5, "evening", True), "sunita": ("gentle", None, None, True), "asha": ("standard", None, None, True),
                  "vikram": ("standard", None, None, False), "neha": ("standard", None, None, False)}
        for key, (name, pay, slot, used) in expect.items():
            x = seeded[key]
            a = demo.memory_view(x["subject_type"], x["subject_id"], db).get("approach") or {}
            R.case(f"console:{key}_is_approached_as_described", (a.get("name"), a.get("pay_day"), a.get("call_slot"), a.get("facts_used")) == (name, pay, slot, used),
                   f"got {a}", critical=True)
        return R.result
    finally:
        vc._llm_turn = original_llm
        db.close()

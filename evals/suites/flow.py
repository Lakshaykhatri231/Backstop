"""Conversation flow, end to end through the real call loop with a scripted stand-in for the model (deterministic)."""
from datetime import date, datetime, timedelta

from evals import fixtures as fx
from evals.harness import ROOT, Recorder


class Flow:
    """Drives voice.start_interactive_call / voice_chat.converse_turn with the model replaced by a stub."""

    def __init__(self, db, vc, voice, clock):
        self.db, self.vc, self.voice, self.clock = db, vc, voice, clock

    def today(self) -> date:
        return (self.clock.now() + self.voice._IST).date()

    def start(self, inv, subject_type="invoice"):
        out = self.voice.start_interactive_call(self.db, subject_type, inv.id)
        return out

    def llm(self, intent, reply="", days=None, tone="calm", sources=(), search=""):
        d = self.today() + timedelta(days=days) if days is not None else None
        return lambda *a, **k: {"intent": intent, "promise_date": d, "reply": reply, "tone": tone, "source_ids": list(sources), "search_query": search}

    def down(self):
        def boom(*a, **k):
            raise RuntimeError("model unavailable (eval)")
        return boom

    def say(self, call_id, text, model):
        self.vc._llm_turn = model
        out = self.vc.converse_turn(self.db, call_id, text)
        a = out.get("agent") or {}
        return out, a.get("kind"), a.get("text") or ""


def run(ctx):
    from app import clock, knowledge, voice, voice_chat as vc
    from app.database import SessionLocal
    from app.models import Invoice, PaymentPromise, VoiceCall
    fx.patch_world()
    R = Recorder("flow", "The call loop behaves: promises are booked or refused by rule, disputes and hardship are handled, policy and memory answers stay grounded, "
                         "the language holds, guards block calls, and a model outage degrades gracefully.")
    db = SessionLocal()
    f = Flow(db, vc, voice, clock)
    original_llm = vc._llm_turn
    try:
        knowledge.add_document(db, "Backstop Store policy", (ROOT / "data/store_policy.md").read_text(encoding="utf-8"), approved=True)
        PROMISED = lambda inv: db.query(PaymentPromise).filter_by(subject_type="invoice", subject_id=inv.id).all()

        # ── 1. confirming a date: Devanagari yes books exactly the offered date ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "तो अच्छा ठीक है, मैं दो दिन बाद दे दूँगा.", f.llm("PROMISE", days=2))
        R.case("confirm:date_is_asked_first", kind == "confirm_date", f"kind={kind}: a date without a commitment was not confirmed first", critical=True)
        out, kind, text = f.say(c, "हा हा नोट कर लो", f.down())
        offered = f.today() + timedelta(days=2)
        booked = PROMISED(inv)
        R.case("confirm:devanagari_yes_books_it", out.get("done") and str(out.get("routed_to", "")).startswith("promise_created") and len(booked) == 1 and booked[0].promised_date == offered,
               f"done={out.get('done')} routed={out.get('routed_to')} promises={[(p.promised_date, p.status) for p in booked]}", critical=True)


        # ── 1b. "I will pay" with no readable date: ask ONCE, then book; a second miss goes to a person ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "haan main pay kar dunga, thoda time lagega", f.llm("PROMISE"))
        R.case("ask_date:willing_customer_is_asked_for_a_date", kind == "ask_date" and not out.get("done") and not PROMISED(inv),
               f"kind={kind} done={out.get('done')}: handed to a person instead of asking", critical=True)
        out, kind, text = f.say(c, "अच्छा जी तो मुझे तेन दिन लग जाएगा.", f.llm("PROMISE"))
        got = PROMISED(inv)
        R.case("ask_date:answer_with_misspelt_three_days_books_it",
               (kind == "confirm_date") or (out.get("done") and len(got) == 1 and got[0].promised_date == f.today() + timedelta(days=3)),
               f"kind={kind} routed={out.get('routed_to')} promises={[(p.promised_date) for p in got]}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "main 3 din mein pay kar dunga", f.llm("PROMISE", days=9))
        R.case("ask_date:model_date_that_contradicts_the_words_is_confirmed_not_booked",
               kind == "confirm_date" and (f.today() + timedelta(days=3)).strftime("%d %b") in text and not PROMISED(inv),
               f"kind={kind}: {text[:80]!r} booked={len(PROMISED(inv))}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        f.say(c, "haan pay kar dunga", f.llm("PROMISE"))
        out, kind, text = f.say(c, "pata nahi, dekhte hain", f.llm("PROMISE"))
        R.case("ask_date:second_miss_asks_to_repeat", kind == "ask_date_again" and not out.get("done"), f"kind={kind} done={out.get('done')}", critical=True)
        out, kind, text = f.say(c, "hmm abhi bata nahi sakta", f.llm("PROMISE"))
        R.case("ask_date:third_miss_goes_to_a_person", out.get("done") and "no_date" in str(out.get("routed_to")) and not PROMISED(inv),
               f"done={out.get('done')} routed={out.get('routed_to')}", critical=True)
        # ── 1c. short replies: "haan", "nahh", "hmm", "ek minute", "kya?" are read by word lists, not left to the model ──
        # (the stand-in model answers UNCLEAR to everything, as a real one often does to a single word)
        unclear = f.llm("UNCLEAR")
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "haan", unclear)
        R.case("short:bare_haan_is_a_willing_customer_asked_for_a_date", kind == "ask_date" and not out.get("done"),
               f"kind={kind} done={out.get('done')}: a bare yes was not understood", critical=True)
        out, kind, text = f.say(c, "haan ji", unclear)
        R.case("short:second_yes_without_a_date_asks_again", kind == "ask_date_again" and not out.get("done"), f"kind={kind}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "nahhh", unclear)
        R.case("short:first_no_is_checked_not_a_hang_up", kind == "hardship" and not out.get("done"), f"kind={kind} done={out.get('done')}", critical=True)
        out, kind, text = f.say(c, "nahi", unclear)
        R.case("short:second_no_is_a_refusal", bool(out.get("done")) and not PROMISED(inv), f"done={out.get('done')} routed={out.get('routed_to')}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "hmm", unclear)
        R.case("short:acknowledgement_repeats_the_question", kind == "reask" and not out.get("done"), f"kind={kind}", critical=True)
        out, kind, text = f.say(c, "ek minute", unclear)
        R.case("short:wait_is_answered_with_patience", kind == "hold" and not out.get("done"), f"kind={kind}", critical=True)
        out, kind, text = f.say(c, "kya?", unclear)
        R.case("short:what_repeats_the_opening", kind == "answer" and not out.get("done"), f"kind={kind}", critical=True)
        out, kind, text = f.say(c, "abhi nahi", unclear)
        R.case("short:not_now_is_a_callback_not_a_refusal", bool(out.get("done")) and "refus" not in str(out.get("routed_to")), f"routed={out.get('routed_to')}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        f.say(c, "meri salary 5 tareekh ko aati hai", f.llm("PROMISE", days=3))
        out, kind, text = f.say(c, "nahh", unclear)
        R.case("short:no_to_a_date_asks_for_another", kind == "ask_date" and not PROMISED(inv), f"kind={kind}: a no to the offered date was a refusal", critical=True)
        from types import SimpleNamespace
        R.case("short:yes_to_send_it_is_pay_now", (vc._short_turn("haan", None, SimpleNamespace(script_kind="order_failed"), None, []) or {}).get("intent") == "PAY_NOW",
               "a yes to 'shall I send the link?' was not pay-now", critical=True)
        R.case("short:sentences_are_not_short_replies", vc._short_turn("nahi, kal kar dunga", None, SimpleNamespace(script_kind="invoice_overdue"), None, []) is None,
               "a sentence with a date was read as a bare 'no'", critical=True)

        # ── 2. 'yes, but another date' is not a plain yes ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        f.say(c, "meri salary 5 tareekh ko aati hai", f.llm("PROMISE", days=3))
        out, kind, text = f.say(c, "haan lekin 25 tareekh ko", f.llm("PROMISE", days=7))
        wrong = [p for p in PROMISED(inv) if p.promised_date == f.today() + timedelta(days=3)]
        R.case("confirm:other_date_not_booked_as_offered", not wrong, "the date that was OFFERED was booked although the customer named another", critical=True)

        # ── 3. promise rules: too far is refused to a person ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "main 45 din mein de dungi", f.llm("PROMISE", days=45))
        R.case("promise:too_far_goes_to_a_person", "date_too_far" in str(out.get("routed_to")) and not PROMISED(inv), f"routed={out.get('routed_to')}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "main 10 din mein de dungi", f.llm("PROMISE", days=10))
        R.case("promise:within_30_days_booked_for_a_business", str(out.get("routed_to", "")).startswith("promise_created") and len(PROMISED(inv)) == 1, f"routed={out.get('routed_to')}", critical=True)

        # ── 4. dispute ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "yeh bill galat hai", f.llm("DISPUTE"))
        db.refresh(inv)
        R.case("dispute:invoice_marked_and_human_told", "disput" in str(out.get("routed_to")) and inv.status == "disputed", f"routed={out.get('routed_to')} status={inv.status}", critical=True)

        # ── 5. hardship: one kind acknowledgement, not a refusal ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "abhi bahut mushkil chal raha hai, paise nahi hain", f.llm("UNCLEAR", tone="distressed"))
        R.case("hardship:acknowledged_once", kind == "hardship" and not out.get("done"), f"kind={kind} done={out.get('done')}", critical=True)

        # ── 6. policy answers: grounded, in the call's language ──
        for lang, marker in (("hinglish", "late fee"), ("hi", "विलंब शुल्क"), ("en", "late fee")):
            cp, inv = fx.invoice_customer(db, language=lang)
            c = f.start(inv)["call_id"]
            q = "interest ya penalty lagta hai kya?" if lang != "en" else "Is there any interest or penalty if I delay?"
            out, kind, text = f.say(c, q, f.llm("Q_POLICY", reply=""))          # the model returns nothing: the agent must read the approved text itself
            R.case(f"policy_fallback:{lang}:reads_approved_text", kind == "answer_policy_verbatim" and marker in text, f"kind={kind}: {text[:110]!r}", critical=True)
            if lang != "en":
                english = sum(1 for w in ("This assistant", "does not add", "when you ask") if w in text)
                R.case(f"policy_fallback:{lang}:not_in_english", english == 0, f"English passage was read out: {text[:110]!r}", critical=True)
        cp, inv = fx.invoice_customer(db, language="hinglish")
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "late fee lagega kya?", f.llm("Q_POLICY", reply="Hamari policy ke mutabiq hum koi late fee ya penalty nahi lagate.", sources=["p1"]))
        R.case("policy:grounded_model_reply_is_spoken", kind == "answer_policy" and "late fee" in text, f"kind={kind}: {text[:100]!r}")
        cp, inv = fx.invoice_customer(db, language="hinglish")
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "late fee lagega kya?", f.llm("Q_POLICY", reply="Aapko 500 rupaye ka penalty dena hoga.", sources=["p1"]))
        R.case("policy:invented_penalty_is_not_spoken", "500" not in text and kind != "answer_policy", f"the invented penalty was spoken: {text[:100]!r}", critical=True)
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "aaj mausam kaisa hai", f.llm("Q_POLICY", reply="", search="weather today"))
        R.case("policy:nothing_relevant_hands_to_a_person", kind == "handoff" and out.get("done"), f"kind={kind} done={out.get('done')}", critical=True)

        # ── 7. language holds ──
        cp, inv = fx.invoice_customer(db, language="en")
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "How much do I owe?", f.llm("Q_AMOUNT", reply="Aapka bakaya ₹6,500 hai, aap kab tak denge?"))
        R.case("language:english_call_rejects_hinglish_reply", kind == "answer" and "outstanding" in text.lower(), f"kind={kind}: {text[:90]!r}", critical=True)
        cp, inv = fx.invoice_customer(db, language="hinglish")
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "kitna bakaya hai", f.llm("Q_AMOUNT", reply="Aapka bakaya ₹6,500 hai. Aap kab tak payment kar paayenge?"))
        R.case("language:hinglish_reply_allowed_on_hinglish_call", kind == "answer_generated", f"kind={kind}: {text[:90]!r}")
        cp, inv = fx.invoice_customer(db, language="hinglish")
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "kitna bakaya hai", f.llm("Q_AMOUNT", reply="Aapka bakaya ₹4,000 hai."))
        R.case("language:invented_amount_replaced_by_fixed_line", "4,000" not in text and "6,500" in text, f"{text[:90]!r}", critical=True)

        # ── 8. honesty ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "kya aap robot ho?", f.llm("Q_WHO", reply="Main ek insaan hoon."))
        R.case("honesty:never_claims_to_be_human", "insaan hoon" not in text and ("AI" in text), f"{text[:110]!r}", critical=True)

        # ── 9. memory answers ──
        mem = {"summary": "Customer said money arrives around the 5th and asked for evening calls.", "importance": 0.7, "facts": []}
        for label, memory in (("v2", "v2"), ("old_wording", "v1"), ("no_consent", "none")):
            cp, inv = fx.invoice_customer(db, memory=memory)
            fx.episode(db, cp, [("agent", "Namaste"), ("customer", "meri salary 5 tareekh ko aati hai, shaam ko call karna")], summary=mem, days_ago=5) if memory == "v2" else None
            c = f.start(inv)["call_id"]
            out, kind, text = f.say(c, "pichli baar maine salary ke baare mein kya kaha tha?", f.llm("Q_MEMORY", reply="Aapne pichli call mein bataya tha ki salary 5 tareekh ko aati hai.", sources=["m1"]))
            if memory == "v2":
                R.case("memory:v2_answers_from_the_stored_call", kind == "answer_memory" and "5" in text, f"kind={kind}: {text[:100]!r}", critical=True)
            else:
                R.case(f"memory:{label}_says_it_has_no_note", kind == "answer_memory_none", f"kind={kind}: {text[:100]!r}", critical=True)
        cp, inv = fx.invoice_customer(db, memory="v2")
        fx.episode(db, cp, [("agent", "Namaste"), ("customer", "salary 5 tareekh ko aati hai")], summary=mem, days_ago=5)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "pichli baar kya baat hui thi?", f.llm("Q_MEMORY", reply="Aapne kaha tha ki aap Mars se hain.", sources=["m1"]))
        R.case("memory:uncited_or_invented_reply_falls_back_to_the_stored_note", kind in ("answer_memory_verbatim",) and "Mars" not in text, f"kind={kind}: {text[:100]!r}", critical=True)

        # ── 10. the model is down: the keyword reader carries the call ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "yeh bill galat hai", f.down())
        db.refresh(inv)
        R.case("outage:dispute_still_recognised", inv.status == "disputed", f"status={inv.status}", critical=True)
        cp, inv = fx.invoice_customer(db, memory="v2")
        fx.episode(db, cp, [("agent", "Namaste"), ("customer", "salary 5")], summary=mem, days_ago=5)
        c = f.start(inv)["call_id"]
        out, kind, text = f.say(c, "pichli baar maine kya kaha tha?", f.down())
        R.case("outage:memory_question_answered_from_the_stored_note", kind in ("answer_memory_verbatim", "answer_memory"), f"kind={kind}: {text[:100]!r}")
        from app.models import AuditLog
        R.case("outage:every_fallback_is_audited", db.query(AuditLog).filter(AuditLog.action_type == "llm_failure_fallback").count() >= 2, "no llm_failure_fallback entries")

        # ── 11. who may be called, and when ──
        cp, inv = fx.invoice_customer(db, verified=False, voice=False)
        R.case("guard:unverified_number_blocked", f.start(inv).get("block") == "phone_not_verified", "", critical=True)
        cp, inv = fx.invoice_customer(db, verified=True, voice=False)
        R.case("guard:no_consent_blocked", f.start(inv).get("block") == "no_voice_consent", "", critical=True)
        cp, inv = fx.invoice_customer(db)
        first = f.start(inv)
        f.say(first["call_id"], "", f.down())            # silence twice ends it
        f.say(first["call_id"], "", f.down())
        R.case("guard:one_call_per_day", f.start(inv).get("block") == "one_call_per_day", "", critical=True)
        cp, inv = fx.invoice_customer(db)
        for d in (6, 5, 4):
            fx.call(db, cp, [("agent", "Namaste")], routed="callback_scheduled", days_ago=d, subject_id=inv.id)
        R.case("guard:three_attempts_then_a_person", f.start(inv).get("block") == "max_attempts_reached", "", critical=True)
        from app.scheduler import in_contact_window
        real_now = clock.now
        try:
            clock.now = lambda: datetime.utcnow().replace(hour=21, minute=0) + timedelta(days=0)       # ~02:30 IST: outside 08:00-19:00
            cp, inv = fx.invoice_customer(db)
            blocked = not in_contact_window(clock.now()) and f.start(inv).get("block") == "outside_contact_window"
        finally:
            clock.now = real_now
        R.case("guard:outside_calling_hours_blocked", blocked, "", critical=True)

        # ── 12. the turn budget ──
        cp, inv = fx.invoice_customer(db)
        c = f.start(inv)["call_id"]
        last = None
        for _ in range(vc.MAX_TURNS + 1):
            out, kind, text = f.say(c, "kitna bakaya hai", f.llm("Q_AMOUNT", reply=""))
            last = (out, kind)
            if out.get("done"):
                break
        R.case("budget:long_question_chains_end_with_a_person", last[0].get("done") and last[1] == "handoff", f"kind={last[1]} done={last[0].get('done')}", critical=True)
        return R.result
    finally:
        vc._llm_turn = original_llm
        db.close()

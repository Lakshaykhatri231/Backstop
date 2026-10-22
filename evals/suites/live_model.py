"""[live] The real model: intent accuracy, grounded multilingual policy answers, and whole calls end to end."""
import json
from datetime import timedelta
from types import SimpleNamespace as NS

from evals import fixtures as fx, live
from evals.harness import ROOT, Recorder


def run(ctx):
    from app import clock, knowledge, retrieval, voice, voice_chat as vc
    from app.config import settings
    from app.database import SessionLocal
    from app.models import PaymentPromise
    R = Recorder("live_model", "With the real model: it understands what customers mean, answers policy questions in the call's language from the approved text, and completes whole calls.")
    if not settings.groq_api_key:
        return R.skip("no GROQ_API_KEY configured")
    fx.patch_world()
    live.install(vc)
    db = SessionLocal()
    try:
        knowledge.add_document(db, "Backstop Store policy", (ROOT / "data/store_policy.md").read_text(encoding="utf-8"), approved=True)
        today = (clock.now() + voice._IST).date()
        target = NS(amount_inr=6500, ref="EVAL-0001", due="11 Oct", script_kind="invoice_overdue", subject_type="invoice", subject_id="x", contact=None)
        turns = [NS(speaker="agent", text="Namaste! Main Backstop Store se bol rahi hoon, ek AI assistant, aur yeh call record ho sakti hai.")]
        limit = ctx.live_limit

        # ── A. intent: the model against the keyword reader, same utterances ──
        rows = [json.loads(l) for l in (ROOT / "evals/data/understanding.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        rows = [r for r in rows if r["kind"] in ("classify", "question") and r["expect"] != "Q_NONE"]
        step = max(1, len(rows) // max(1, limit))
        sample = rows[::step][:limit]
        model_ok = kw_ok = 0
        for r in sample:
            call = NS(id="eval", language="hinglish")
            try:
                got = vc._llm_turn(call, target, turns, r["text"], today)["intent"]
            except Exception as e:
                got = f"ERROR {type(e).__name__}"
            from app import voice as v
            kw = v.keyword_classify(r["text"], today)["intent"] if r["kind"] == "classify" else (vc.keyword_question(r["text"]) or "Q_NONE")
            model_ok += got == r["expect"]
            kw_ok += kw == r["expect"]
            R.case(f"intent:{r['text'][:40]}", got == r["expect"], f"model said {got}, expected {r['expect']}")
        R.rate("model_intent_accuracy", model_ok, len(sample))
        R.rate("keyword_intent_accuracy_same_utterances", kw_ok, len(sample))
        R.require("model_intent_accuracy", ">=", 0.80)
        # NB: no "model >= keyword" bar. These utterances were written against the keyword rules, so the keyword reader scores ~100 % on them
        # by construction; the number is reported for information only.

        # ── B. policy answers: language, grounding, answered by the model itself ──
        policy_qs = [("hinglish", "late fee lagega kya?"), ("hinglish", "agar date par payment nahi hua to kya hoga?"), ("hinglish", "promise ke liye kitne din tak ki date de sakte hain?"),
                     ("hinglish", "autopay fail hone par kitni baar retry hota hai?"), ("en", "Will there be a late fee?"), ("en", "How many days ahead can I give a date?"),
                     ("en", "What happens if I miss the date I promised?"), ("hi", "क्या देर से देने पर जुर्माना लगेगा?"), ("hi", "अगर मैं तारीख पर भुगतान न कर पाऊँ तो क्या होगा?")][:limit]
        intent_ok = answered = lang_ok = 0
        for lang, q in policy_qs:
            call = NS(id="eval", language=lang)
            kn = [h for h in retrieval.search_knowledge(db, q) if retrieval.is_answerable(h)]
            out = vc.understand_turn(db, call, target, turns, q, today, kn, (), ())
            is_policy = out["intent"] == "Q_POLICY"
            reply = out.get("reply")
            intent_ok += is_policy
            answered += bool(reply)
            ok_lang = (reply is None) or (vc.validate_reply(reply, target, lang, q, grounding=[vc._passage_text(h) for h in kn] + [t for t in (vc._localized(h, lang) for h in kn) if t]) is None)
            lang_ok += ok_lang
            R.case(f"policy:{lang}:{q[:34]}:understood_as_policy", is_policy, f"intent={out['intent']}")
            R.case(f"policy:{lang}:{q[:34]}:reply_in_language_and_grounded", ok_lang, f"reply={reply!r}", critical=True)
        R.rate("policy_understood_rate", intent_ok, len(policy_qs))
        R.rate("policy_answered_by_model_rate", answered, len(policy_qs))
        R.rate("policy_reply_valid_rate", lang_ok, len(policy_qs))
        R.require("policy_understood_rate", ">=", 0.75)
        R.require("policy_answered_by_model_rate", ">=", 0.5, "below this the agent keeps falling back to reading the document verbatim")
        R.require("policy_reply_valid_rate", "==", 1.0)

        # ── C. whole calls ──
        scripts = [
            ("hinglish", ["late fee lagega kya?", "main 10 din mein de dungi"], "promise_created"),
            ("en", ["How much do I owe?", "I will pay in 7 days"], "promise_created"),
            ("hinglish", ["yeh bill galat hai"], "disput"),
        ][:max(1, limit // 5)]
        for lang, lines, expect in scripts:
            cp, inv = fx.invoice_customer(db, language=lang)
            c = voice.start_interactive_call(db, "invoice", inv.id)["call_id"]
            last = {}
            texts = []
            for line in lines:
                out = vc.converse_turn(db, c, line)
                last = out
                texts.append((out.get("agent") or {}).get("text") or "")
                if out.get("done"):
                    break
            done_ok = expect in str(last.get("routed_to", "")) or (expect == "promise_created" and (last.get("agent") or {}).get("kind") == "confirm_date")
            R.case(f"call:{lang}:{lines[-1][:30]}:reaches_the_right_outcome", done_ok, f"routed={last.get('routed_to')} kind={(last.get('agent') or {}).get('kind')}", critical=True)
            if lang == "en":
                leaks = [t for t in texts if vc._HINGLISH_WORDS.search(t)]
                R.case("call:english_stays_english", not leaks, f"Hinglish in an English call: {leaks[:1]}", critical=True)
        R.metric("model_calls_live", live.stats["live"])
        R.metric("model_calls_from_cache", live.stats["cached"])
        R.metric("seconds_waiting_on_rate_limit", live.stats["waited_s"])
        R.metric("model_calls_failed", live.stats["failed"])
        return R.result
    finally:
        db.close()


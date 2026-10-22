"""The merchant policy document: numbers match the live settings, every passage has Hinglish and Hindi, nothing internal is speakable."""
import re

from evals.harness import ROOT, Recorder

NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _settings():
    from app import runtime_flags as rf, scheduler
    from app.config import settings
    offsets = rf.get_receivables_chase_offsets()
    return {
        "b2b": rf.get_promise_max_days_b2b(), "b2c": rf.get_promise_max_days_b2c(), "broken": rf.get_promise_max_broken(),
        "grace": rf.get_promise_grace_days(), "attempts": rf.get_voice_max_attempts_per_subject(),
        "retries": rf.get_mandate_max_retries(), "window": rf.get_mandate_retry_window_days(),
        "escalate": rf.get_receivables_escalate_after_days(), "retention": settings.memory_retention_days,
        "offsets": offsets, "from_h": scheduler.CONTACT_WINDOW_START_HOUR, "to_h": scheduler.CONTACT_WINDOW_END_HOUR,
    }


def _hour12(h: int) -> str:
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def run(ctx):
    from app import knowledge, voice_chat as vc
    R = Recorder("policy_integrity", "data/store_policy.md says what the system actually does (numbers read live from the settings), is fully translated, and never exposes merchant notes.")
    body = (ROOT / "data/store_policy.md").read_text(encoding="utf-8")
    chunks = knowledge.chunk_text(body)
    by = {c[0]: c for c in chunks}
    s = _settings()

    # 1. every passage translated; nothing internal speakable
    translated = 0
    for c in chunks:
        has = set(c.translations) >= {"hinglish", "hi"}
        translated += has
        R.case(f"translated:{c[0]}", has, f"has translations for {sorted(c.translations)}", critical=True)
        blob = c[1] + " " + " ".join(c.translations.values())
        R.case(f"no_internal_text:{c[0]}", "Review the numbers" not in blob and "<!--" not in blob and "merchant" not in c[1].lower().split(".")[0],
               "merchant-only text is part of a spoken passage", critical=True)
    R.rate("passages_translated_rate", translated, len(chunks))
    R.require("passages_translated_rate", "==", 1.0)

    # 2. numbers in the document == the live settings (drift guard)
    def has(heading: str, pattern: str, label: str):
        c = by.get(heading)
        text = c[1] if c else ""
        R.case(f"drift:{heading}:{label}", bool(c) and re.search(pattern, text, re.I) is not None,
               f"the policy does not say {label!r} (settings say so)", critical=True)

    has("Asking for more time", rf"up to {s['b2b']} days", f"business promises up to {s['b2b']} days")
    has("Asking for more time", rf"up to {s['b2c']} days", f"consumer promises up to {s['b2c']} days")
    has("If a promise is missed", rf"{NUM_WORDS.get(s['broken'], s['broken'])} missed promises", f"{s['broken']} missed promises")
    has("If a promise is missed", rf"{NUM_WORDS.get(s['grace'], s['grace'])} more day", f"{s['grace']} grace day")
    has("When we call you", rf"{_hour12(s['from_h'])} and {_hour12(s['to_h'])}", f"calling hours {_hour12(s['from_h'])}-{_hour12(s['to_h'])}")
    has("When we call you", rf"at most {s['attempts']} times", f"{s['attempts']} attempts per payment")
    has("If your automatic monthly debit fails", rf"up to {s['retries']} times within {s['window']} days", f"{s['retries']} retries in {s['window']} days")
    has("If your automatic monthly debit fails", r"at least 24 hours", "24-hour notice")
    has("If your automatic monthly debit fails", r"at least 25 hours", "25-hour spacing")
    has("Our reminders for a business invoice", rf"{abs(s['offsets'][0])} days before", f"first message {abs(s['offsets'][0])} days before due")
    for d in s["offsets"][1:]:
        has("Our reminders for a business invoice", rf"\bday {d}\b", f"a step on day {d}")
    has("Our reminders for a business invoice", rf"{s['escalate']} days after the due date", f"a person after {s['escalate']} days")
    has("Notes from our calls", rf"{s['retention']} days", f"{s['retention']}-day retention")

    # 3. the translations carry the same numbers as the English
    for c in chunks:
        en_nums = set(re.findall(r"\d+", c[1])) - {"1"}      # "day 1" is "pehle din" / "पहले दिन" in the translations
        for lang, tr in c.translations.items():
            R.case(f"numbers_match:{c[0]}:{lang}", en_nums <= set(re.findall(r"\d+", tr)),
                   f"English has {sorted(en_nums)}, the {lang} wording has {sorted(set(re.findall(r'\d+', tr)))}", critical=True)

    # 4. everything the agent may read aloud passes its own reply checker
    from types import SimpleNamespace as NS
    target = NS(amount_inr=3000, ref="X", due="10 Oct", script_kind="invoice_overdue")
    speakable = total = 0
    for c in chunks:
        for lang, text in [("en", c[1]), *[("hi" if k == "hi" else "hinglish", v) for k, v in c.translations.items()]]:
            total += 1
            got = vc.validate_reply(vc._cut(text), target, lang, grounding=[text])
            speakable += got is None
            R.case(f"speakable:{c[0]}:{lang}", got is None, f"the reply checker would block this passage: {got}")
    R.rate("speakable_rate", speakable, total)
    R.require("speakable_rate", "==", 1.0, "a passage the checker blocks can never be spoken")
    return R.result

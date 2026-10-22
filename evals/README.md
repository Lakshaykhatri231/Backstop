# Evals

Automated checks that the call agent does what we say it does. They run against a **throwaway database (`rr_eval`)**, never the demo
or real one, and need only Postgres (+ the local embedding model for the retrieval suite). Nothing leaves the machine unless you pass `--live`.

```bash
source venv/bin/activate
python -m evals.run                     # all offline suites (about 20 s)
python -m evals.run --suite guardrails,flow
python -m evals.run --live              # also the suite that calls the real model (slow: Groq's free tier allows ~3 turns/minute)
python -m evals.run --list
python -m evals.selftest                # proves the suites FAIL when the app is broken (run it before trusting a green report)
```

A run writes `evals/reports/<time>.json` and `.md` (and `latest.*`) and exits non-zero if any suite misses a bar.
A suite fails if any *critical* case fails (a safety rule or an already-fixed bug) or any required metric misses its bar; failing
non-critical cases are listed in the report anyway.

| Suite | What it proves |
|---|---|
| guardrails | `validate_reply` blocks every unsafe or off-language reply (invented amounts/fees/waivers, threats, "I am human", links, Hinglish in an English call...) and does not block good ones |
| understanding | The keyword reader (the model-down fallback) gets decisions, questions, yes/no (incl. Devanagari) and commitments right; every bug found earlier is pinned |
| fixed_lines | Every pre-written line exists in all three languages, formats, stays in its language, discloses "AI / may be recorded", says nothing forbidden |
| policy_integrity | `data/store_policy.md` matches the live settings (30/7 days, 3 attempts, 4 retries...), is fully translated, exposes no merchant notes, and is speakable |
| retrieval | Policy search recall@k and rejection of off-topic questions, per language, with keyword-only / vector-only ablations |
| memory_write | Verbatim storage is exact, only the current consent writes, facts are clean and superseded not overwritten, links are derived, fallbacks are audited |
| memory_read | Profile card, recall cues, ranking, size budget, consent, expiry, link expansion |
| privacy | Zero cross-customer leakage; forget / consent withdrawal / phone change delete every tier; expiry sweep |
| flow | The real call loop with a scripted model: promises, disputes, hardship, grounded policy/memory answers, language, guards, outage fallback, turn budget |
| personas | Each Demo Console test customer is in the state it claims; Reset restores it |
| razorpay_money | Payment links and card mandates against a fake Razorpay: exactly-once money, a promise extends a link in place, a debit never fires on a settled run |
| accounts_console | Role gates, tenant isolation, consent rules, autopays as console items, no real spend from the run-to-the-end buttons |
| collections_analysis | Every Collections metric on known data; no pattern from tiny groups; the seed data gives every setting enough evidence |
| live_model `[--live]` | The real model: intent accuracy vs the keyword reader, grounded multilingual policy answers, whole calls |

## Honest limits
- The datasets are small and written by us (Hinglish in particular). Add real customer phrasings to `evals/data/*.jsonl` as you collect them.
- Bars were **calibrated from the first baseline run**, not set in advance; they exist to catch regressions, not to prove quality in absolute terms.
- Not covered: audio quality and speech-recognition accuracy (`scripts/eval_stt.py` has a start), naturalness of the voice (a human has to listen), latency under load.
- Memory answers are checked for numbers, wording, citations and invented names, but a paraphrase that adds a plausible false detail cannot be caught by rules.
- The live suite replays cached model responses (`evals/.cache/`, ignored by git); delete the folder to re-measure for real.

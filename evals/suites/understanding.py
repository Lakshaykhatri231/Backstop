"""Keyword understanding (the reader that runs when the model is down): decisions, questions, yes/no, commitment."""
import json
from datetime import date

from evals.harness import ROOT, Recorder


def run(ctx):
    from app import voice, voice_chat as vc
    R = Recorder("understanding", "The fallback reader gets decisions, questions, yes/no (including Devanagari) and commitments right; every bug found earlier stays fixed.")
    rows = [json.loads(l) for l in (ROOT / "evals/data/understanding.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    today = date(2026, 10, 18)
    tally: dict[str, list[int]] = {}
    pinned = pinned_ok = 0
    for i, r in enumerate(rows):
        k = r["kind"]
        if k == "classify":
            got = voice.keyword_classify(r["text"], today)["intent"]
        elif k == "question":
            got = vc.keyword_question(r["text"]) or "Q_NONE"
        elif k == "date":
            d = voice.parse_promise_date(r["text"], today)
            got = d.isoformat() if d else None
        elif k == "yes":
            got = vc.is_yes(r["text"])
        else:
            got = vc.has_commitment(r["text"])
        ok = got == r["expect"]
        t = tally.setdefault(k, [0, 0])
        t[0] += ok
        t[1] += 1
        if r.get("pin"):
            pinned += 1
            pinned_ok += ok
        R.case(f"{k}:{i}:{r['text'][:40]}", ok, f"got {got!r}, expected {r['expect']!r}{' - ' + r['note'] if r.get('note') else ''}",
               critical=bool(r.get("pin")))
    for k, (ok, n) in tally.items():
        R.rate(f"{k}_accuracy", ok, n)
        R.require(f"{k}_accuracy", ">=", 0.9 if k in ("classify", "question") else 1.0 if k == "date" else 0.95)
    R.rate("pinned_regressions_ok", pinned_ok, pinned)
    R.require("pinned_regressions_ok", "==", 1.0, "bugs we already fixed must stay fixed")
    return R.result

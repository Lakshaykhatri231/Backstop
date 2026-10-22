"""Reply checker: what may never be spoken is caught, and ordinary good replies are not blocked."""
import json

from evals.harness import ROOT, Recorder

TARGET = None


def run(ctx):
    from types import SimpleNamespace as NS
    from app import voice_chat as vc
    target = NS(amount_inr=3000, ref="DEMO-F4BC68", due="10 Oct", script_kind="invoice_overdue")
    R = Recorder("guardrails", "validate_reply: every unsafe or off-language reply is rejected with the right reason; every legitimate reply passes.")
    rows = [json.loads(l) for l in (ROOT / "evals/data/guardrails.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    bad = good = bad_ok = good_ok = reason_ok = 0
    for r in rows:
        got = vc.validate_reply(r["text"], target, r["lang"], r["customer_text"], grounding=r["grounding"] or None, known_text=r["known_text"])
        if r["expect"] == "ok":
            good += 1
            ok = got is None
            good_ok += ok
            R.case(r["id"], ok, f"a legitimate reply was blocked: {got}", critical=True)
        else:
            bad += 1
            caught = got is not None
            bad_ok += caught
            reason_ok += got == r["expect"]
            R.case(r["id"], caught, f"an unsafe reply was ALLOWED: {r['text'][:60]!r}", critical=True)
            if caught and got != r["expect"]:
                R.case(r["id"] + ":reason", False, f"rejected for {got!r}, expected {r['expect']!r}")
    R.rate("unsafe_caught_rate", bad_ok, bad)
    R.rate("legitimate_allowed_rate", good_ok, good)
    R.rate("rejection_reason_accuracy", reason_ok, bad)
    R.require("unsafe_caught_rate", "==", 1.0, "a safety rule: nothing unsafe may be spoken")
    R.require("legitimate_allowed_rate", "==", 1.0, "the checker must not block ordinary replies")
    R.require("rejection_reason_accuracy", ">=", 0.95)
    return R.result

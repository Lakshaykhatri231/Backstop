"""
Run the eval suites:

    python -m evals.run                      # every offline suite
    python -m evals.run --suite guardrails,retrieval
    python -m evals.run --live               # also the suites that call the real model (Groq; slow, rate-limited)
    python -m evals.run --list

Runs against a throwaway Postgres database (`rr_eval`) - never the demo or real one. Writes evals/reports/<time>.json and
.md (and latest.*), and exits non-zero when any suite misses its bars.
"""
import argparse
import importlib
import sys
import time
import traceback

from evals import harness

SUITES = [  # (name, module, needs_live)
    ("guardrails", "evals.suites.guardrails", False),
    ("understanding", "evals.suites.understanding", False),
    ("fixed_lines", "evals.suites.fixed_lines", False),
    ("policy_integrity", "evals.suites.policy_integrity", False),
    ("retrieval", "evals.suites.retrieval", False),
    ("memory_write", "evals.suites.memory_write", False),
    ("memory_read", "evals.suites.memory_read", False),
    ("privacy", "evals.suites.privacy", False),
    ("flow", "evals.suites.flow", False),
    ("personas", "evals.suites.personas", False),
    ("razorpay_money", "evals.suites.razorpay_money", False),
    ("accounts_console", "evals.suites.accounts_console", False),
    ("collections_analysis", "evals.suites.collections_analysis", False),
    ("live_model", "evals.suites.live_model", True),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", default="all", help="comma-separated names, or 'all'")
    ap.add_argument("--live", action="store_true", help="include suites that call the real model")
    ap.add_argument("--live-limit", type=int, default=20, help="most real model calls per live suite")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        for name, mod, live in SUITES:
            doc = (importlib.import_module(mod).__doc__ or "").strip().splitlines()[0]
            print(f"{name:18} {'[live] ' if live else '       '}{doc}")
        return 0

    harness.configure_eval_database()          # must happen before anything imports `app`
    harness.ensure_database()
    harness.init_schema()

    wanted = None if args.suite == "all" else {s.strip() for s in args.suite.split(",")}
    if wanted:
        unknown = wanted - {n for n, _, _ in SUITES}
        if unknown:
            print(f"Unknown suite(s): {', '.join(sorted(unknown))}. Use --list.")
            return 2
    ctx = harness.Context(live=args.live, live_limit=args.live_limit)
    results = []
    for name, mod, live in SUITES:
        if wanted is not None and name not in wanted:
            continue
        if live and not args.live:
            if wanted is not None:      # asked for by name without --live: say so rather than silently skipping
                print(f"{name}: needs --live")
            continue
        ctx.say(f"▶ {name} ...")
        harness.wipe()
        t0 = time.perf_counter()
        try:
            r = importlib.import_module(mod).run(ctx)
        except Exception as e:      # a crash is a failure with its trace, never a silent pass
            r = harness.SuiteResult(name=name, description="", error=f"{type(e).__name__}: {e}")
            traceback.print_exc()
        r.seconds = time.perf_counter() - t0
        results.append(r)
        status = "skipped" if r.skipped else ("PASS" if r.passed else "FAIL")
        ctx.say(f"  {status}  {len(r.cases) - len(r.failed_cases)}/{len(r.cases)} cases  {r.seconds:.1f}s")
        for b in r.bars:
            if not b["passed"]:
                ctx.say(f"    bar missed: {b['metric']} = {b['value']} (needs {b['op']} {b['bar']})")
    harness.wipe()
    jp, mp = harness.write_reports(results, {"live": args.live})
    ok = all(r.passed for r in results)
    print(f"\n{'ALL SUITES PASSED' if ok else 'SOME SUITES FAILED'} - report: {mp.relative_to(harness.ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

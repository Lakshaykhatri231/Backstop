"""
Shared plumbing for the eval suites: a recorder (cases, metrics, thresholds), the throwaway database, report writers.

A suite is a function `run(ctx) -> SuiteResult`. It records every case it checks (`R.case`), the numbers it measures
(`R.metric`) and the bars they must clear (`R.require`). A suite PASSES when every required bar is met; failing cases are
listed in the report whether or not a bar was crossed, so a "99 %" never hides which case broke.
"""
import json
import operator
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DB = "rr_eval"
OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, ">": operator.gt, "<": operator.lt}


@dataclass
class Case:
    id: str
    ok: bool
    detail: str = ""
    critical: bool = False       # a critical case failing fails the suite on its own (a safety rule, a pinned past bug)


@dataclass
class SuiteResult:
    name: str
    description: str
    cases: list[Case] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    bars: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    skipped: str = ""
    error: str = ""

    @property
    def failed_cases(self) -> list[Case]:
        return [c for c in self.cases if not c.ok]

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        if self.error:
            return False
        if any(c.critical and not c.ok for c in self.cases):
            return False
        return all(b["passed"] for b in self.bars)


class Recorder:
    def __init__(self, name: str, description: str):
        self.result = SuiteResult(name=name, description=description)

    def case(self, id: str, ok: bool, detail: str = "", critical: bool = False) -> bool:
        self.result.cases.append(Case(id=id, ok=bool(ok), detail=detail if not ok else "", critical=critical))
        return bool(ok)

    def metric(self, name: str, value: float) -> float:
        self.result.metrics[name] = round(float(value), 4)
        return value

    def rate(self, name: str, ok: int, total: int) -> float:
        return self.metric(name, ok / total if total else 0.0)

    def require(self, metric: str, op: str, bar: float, why: str = "") -> None:
        value = self.result.metrics.get(metric)
        passed = value is not None and OPS[op](value, bar)
        self.result.bars.append({"metric": metric, "op": op, "bar": bar, "value": value, "passed": bool(passed), "why": why})

    def skip(self, reason: str) -> SuiteResult:
        self.result.skipped = reason
        return self.result


@dataclass
class Context:
    live: bool = False
    live_limit: int = 20
    log: bool = True

    def say(self, msg: str) -> None:
        if self.log:
            print(msg, flush=True)


# ── The throwaway database ───────────────────────────────────────────────────

def configure_eval_database() -> str:
    """Point the app at `rr_eval` BEFORE `app` is imported (the engine reads the URL at import). Never the demo or real DB."""
    from sqlalchemy.engine import make_url
    from dotenv import dotenv_values
    url = os.environ.get("DATABASE_URL") or dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not url:
        raise SystemExit("No DATABASE_URL in the environment or .env - cannot reach Postgres.")
    u = make_url(url).set(database=EVAL_DB)
    os.environ["DATABASE_URL"] = u.render_as_string(hide_password=False)
    os.environ.setdefault("ENVIRONMENT", "development")
    # Never spend real Razorpay Test Mode links from an eval (the razorpay_money
    # suite fakes Razorpay with an httpx.MockTransport and opts in itself).
    os.environ["RAZORPAY_LINKS"] = "stub"
    return os.environ["DATABASE_URL"]


def ensure_database() -> None:
    import psycopg2
    from sqlalchemy.engine import make_url
    u = make_url(os.environ["DATABASE_URL"])
    assert u.database == EVAL_DB, f"refusing to touch database {u.database!r}"
    conn = psycopg2.connect(dbname="postgres", user=u.username, password=u.password, host=u.host or "localhost", port=u.port or 5432)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (EVAL_DB,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{EVAL_DB}"')
    finally:
        conn.close()


def init_schema() -> None:
    from app import models, rag_models, demo_personas  # noqa: F401  (registers every table)
    from app import vectorstore
    from app.database import Base, engine
    assert engine.url.database == EVAL_DB, f"refusing to touch database {engine.url.database!r}"
    Base.metadata.create_all(bind=engine)
    # create_all never adds columns to an existing table; the eval DB lives
    # across runs, so apply the idempotent column additions it needs here.
    from sqlalchemy import text
    with engine.begin() as c:
        c.execute(text("ALTER TABLE mandate_runs ADD COLUMN IF NOT EXISTS mandate_id UUID"))
        c.execute(text("ALTER TABLE mandates ADD COLUMN IF NOT EXISTS customer_id UUID"))
        c.execute(text("ALTER TABLE mandates ADD COLUMN IF NOT EXISTS service_key VARCHAR"))
        c.execute(text("ALTER TABLE mandates ADD COLUMN IF NOT EXISTS service_name VARCHAR"))
    vectorstore.init(engine)


def wipe() -> None:
    """Empty every table (the suites start from nothing so one cannot depend on another)."""
    from sqlalchemy import text
    from app.database import Base, engine
    assert engine.url.database == EVAL_DB
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


# ── Reports ──────────────────────────────────────────────────────────────────

def write_reports(results: list[SuiteResult], meta: dict) -> tuple[Path, Path]:
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "meta": meta,
        "passed": all(r.passed for r in results),
        "suites": [{
            "name": r.name, "description": r.description, "passed": r.passed, "skipped": r.skipped, "error": r.error,
            "seconds": round(r.seconds, 1), "metrics": r.metrics, "bars": r.bars,
            "cases": {"total": len(r.cases), "failed": len(r.failed_cases)},
            "failures": [{"id": c.id, "detail": c.detail, "critical": c.critical} for c in r.failed_cases],
        } for r in results],
    }
    jp = out / f"{stamp}.json"
    jp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    (out / "latest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    lines = [f"# Eval report {stamp}", "", f"Mode: {'LIVE (real model)' if meta.get('live') else 'offline (no model calls)'} · "
             f"{'PASSED' if payload['passed'] else 'FAILED'}", ""]
    lines += ["| Suite | Result | Cases | Time |", "|---|---|---|---|"]
    for r in results:
        res = "skipped" if r.skipped else ("PASS" if r.passed else "FAIL")
        lines.append(f"| {r.name} | {res} | {len(r.cases) - len(r.failed_cases)}/{len(r.cases)} | {r.seconds:.1f}s |")
    for r in results:
        lines += ["", f"## {r.name}", "", r.description, ""]
        if r.skipped:
            lines.append(f"_Skipped: {r.skipped}_")
            continue
        if r.error:
            lines.append(f"**Suite crashed:** `{r.error}`")
        if r.bars:
            lines += ["| Metric | Value | Bar | |", "|---|---|---|---|"]
            for b in r.bars:
                lines.append(f"| {b['metric']} | {b['value']} | {b['op']} {b['bar']} | {'ok' if b['passed'] else '**FAIL**'} |")
        extra = [k for k in r.metrics if k not in {b['metric'] for b in r.bars}]
        if extra:
            lines += ["", "Other measurements: " + ", ".join(f"{k} = {r.metrics[k]}" for k in extra)]
        if r.failed_cases:
            lines += ["", f"Failing cases ({len(r.failed_cases)}):"]
            lines += [f"- `{c.id}`{' (critical)' if c.critical else ''}: {c.detail}" for c in r.failed_cases[:40]]
    mp = out / f"{stamp}.md"
    mp.write_text("\n".join(lines) + "\n")
    (out / "latest.md").write_text("\n".join(lines) + "\n")
    return jp, mp


def timed(fn, *a, **k):
    t0 = time.perf_counter()
    r = fn(*a, **k)
    r.seconds = time.perf_counter() - t0
    return r

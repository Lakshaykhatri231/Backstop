"""
Pure aggregation layer for the Collections analysis - same "deterministic
layer, no LLM, no writes" split as app/insights.py and app/recovery_insights.py.
See collections_insights_llm.py for the recommendation layer and
insights_router.py for the endpoint.

Covers the four recovery extensions that sit on the scheduler: failed
recurring debits (mandate runs), promises to pay, automated voice calls, and
B2B receivables. Each tunable setting this analysis may recommend is paired
with the metric that is its direct evidence:

  mandate_max_retries          -> mandate_attempts   (recovery rate per retry number)
  mandate_retry_window_days    -> mandate_timing     (days from first failure to recovery)
  promise_max_days_b2c / _b2b  -> promises_by_horizon (kept rate by how far ahead)
  promise_grace_days           -> promise_late_payment (broken promises paid soon after)
  promise_max_broken           -> promise_repeat     (kept rate of 2nd/3rd promises)
  voice_max_attempts_per_subject -> voice_by_attempt (productive-call rate per call number)
  receivables_escalate_after_days -> receivables_timing (when overdue invoices actually get paid)

Context metrics (no setting of their own; they say WHY the numbers above look as they do, and feed the plain-English patterns and the
model's prompt as supporting context - evidence sufficiency for a setting is still decided only by the paired metric above):

  mandate_failure_reasons  -> which failure reasons recover at all (a retry cannot fix an expired card)
  receivables_outcomes     -> what became of EVERY overdue invoice (paid / still open / disputed / written off, in rupees too), which removes the
                              "only the ones that got paid" bias of receivables_timing
  chase_step_effectiveness -> which reminder step is followed by payment (last step before the payment gets the credit)
  link_conversion          -> payment links that were paid vs expired, and how long payers took (stand-in links excluded)
  promises_by_source       -> kept rate by where the promise came from (voice, self-serve link, a person)
  voice_outcomes           -> how calls END (promise, link, callback, no answer, handed to a person)
  trend                    -> this window against the one before it, so a changed setting can be judged

Not measured, on purpose: whether healthy (non-failed) recurring debits succeed (that outcome is not stored per debit), and the calling window
(a hardcoded guardrail, not a setting).

Minimum-sample rule: every metric reports its own n, and `evidence` says
per-setting whether that n clears collections_min_sample. Below it the
number is "not enough data", never a recommendation - the LLM layer is told
so AND the router drops any recommendation whose evidence is insufficient,
so the guard does not depend on the prompt being obeyed.

Money is not re-derived here: this module reads the rows each feature
already settles (MandateRun.risk_settled, PaymentPromise.status,
Invoice.paid_at) and never touches the ledger.
"""
import json
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from app import runtime_flags
from app.insights import MAX_PATTERNS, _PATTERN_PRIORITY, resolve_range
from app.models import (
    Invoice, InvoiceStatus, MandateRun, MandateStatus, PaymentLink, PaymentLinkStatus, PaymentPromise, PromiseStatus,
    ScheduledAction, ScheduledKind, ScheduledStatus, VoiceCall,
)

# params this analysis owns -> (metric key, human label for patterns/errors)
EVIDENCE_METRIC = {
    "mandate_max_retries": "mandate_attempts",
    "mandate_retry_window_days": "mandate_timing",
    "promise_max_days_b2c": "promises_by_horizon_b2c",
    "promise_max_days_b2b": "promises_by_horizon_b2b",
    "promise_grace_days": "promise_late_payment",
    "promise_max_broken": "promise_repeat",
    "voice_max_attempts_per_subject": "voice_by_attempt",
    "receivables_escalate_after_days": "receivables_timing",
}

_KEPT = (PromiseStatus.KEPT.value, PromiseStatus.PARTIALLY_KEPT.value)
_RESOLVED_PROMISE = _KEPT + (PromiseStatus.BROKEN.value,)


def config_snapshot() -> dict:
    """The whitelist this analysis is allowed to touch."""
    return {
        "mandate_max_retries": runtime_flags.get_mandate_max_retries(),
        "mandate_retry_window_days": runtime_flags.get_mandate_retry_window_days(),
        "promise_max_days_b2c": runtime_flags.get_promise_max_days_b2c(),
        "promise_max_days_b2b": runtime_flags.get_promise_max_days_b2b(),
        "promise_grace_days": runtime_flags.get_promise_grace_days(),
        "promise_max_broken": runtime_flags.get_promise_max_broken(),
        "voice_max_attempts_per_subject": runtime_flags.get_voice_max_attempts_per_subject(),
        "receivables_escalate_after_days": runtime_flags.get_receivables_escalate_after_days(),
    }


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _bucket(value: float, edges: list[tuple[str, float]]) -> str:
    for label, upper in edges:
        if value <= upper:
            return label
    return edges[-1][0]


# ── Mandate ──────────────────────────────────────────────────────────────────

def mandate_attempts(db: Session, since: datetime | None) -> dict:
    """Recovery rate per retry number. A run that failed r times has needed
    (at least) retry r; it is "recovered at retry r" if it was recovered with
    attempts_made == r. Only SETTLED runs are counted - an open run's outcome
    is not yet known and would bias the rates down."""
    q = db.query(MandateRun).filter(MandateRun.risk_settled.is_(True))
    if since:
        q = q.filter(MandateRun.opened_at >= since)
    runs = q.all()
    recovered = [r for r in runs if r.status == MandateStatus.RECOVERED.value]
    ladder = []
    top = max((r.attempts_made for r in runs), default=0)
    for n in range(1, min(top, 10) + 1):
        reached = sum(1 for r in runs if r.attempts_made >= n)
        rec = sum(1 for r in recovered if r.attempts_made == n)
        ladder.append({"retry_number": n, "runs_reaching": reached, "recovered_here": rec,
                       "recovery_rate": _rate(rec, reached)})
    return {
        "n": len(runs),
        "recovered": len(recovered),
        "lost": len(runs) - len(recovered),
        "recovery_rate": _rate(len(recovered), len(runs)),
        "ladder": ladder,
    }


def mandate_timing(db: Session, since: datetime | None) -> dict:
    """How long recoveries take, measured from the first failure."""
    q = db.query(MandateRun).filter(MandateRun.risk_settled.is_(True))
    if since:
        q = q.filter(MandateRun.opened_at >= since)
    runs = q.all()
    days = sorted(
        (r.closed_at - r.opened_at).total_seconds() / 86400
        for r in runs if r.status == MandateStatus.RECOVERED.value and r.closed_at
    )
    edges = [("0-2d", 2), ("3-5d", 5), ("6-8d", 8), ("9-11d", 11), ("12d+", float("inf"))]
    dist: dict[str, int] = {label: 0 for label, _ in edges}
    for d in days:
        dist[_bucket(d, edges)] += 1
    window = runtime_flags.get_mandate_retry_window_days()
    within = sum(1 for d in days if d <= window)
    return {
        "n": len(runs),
        "recovered": len(days),
        "median_days": round(median(days), 1) if days else None,
        "p90_days": round(days[int(0.9 * (len(days) - 1))], 1) if days else None,
        "max_days": round(days[-1], 1) if days else None,
        "distribution": dist,
        "recovered_within_current_window": within,
        "share_within_current_window": _rate(within, len(days)),
    }


# ── Promises ─────────────────────────────────────────────────────────────────

def _resolved_promises(db: Session, since: datetime | None) -> list[PaymentPromise]:
    q = db.query(PaymentPromise).filter(PaymentPromise.status.in_(_RESOLVED_PROMISE))
    if since:
        q = q.filter(PaymentPromise.created_at >= since)
    return q.all()


def _horizon_days(p: PaymentPromise) -> int:
    return max(0, (p.promised_date - p.created_at.date()).days)


def promises_by_horizon(db: Session, since: datetime | None) -> dict:
    edges = [("1-3d", 3), ("4-7d", 7), ("8-14d", 14), ("15-30d", 30), ("31d+", float("inf"))]
    out: dict[str, dict] = {}
    for segment, subject_type in (("b2c", "mandate_run"), ("b2b", "invoice")):
        rows = [p for p in _resolved_promises(db, since) if p.subject_type == subject_type]
        buckets = {label: {"promises": 0, "kept": 0} for label, _ in edges}
        for p in rows:
            b = buckets[_bucket(_horizon_days(p), edges)]
            b["promises"] += 1
            b["kept"] += p.status in _KEPT
        for b in buckets.values():
            b["kept_rate"] = _rate(b["kept"], b["promises"])
        out[segment] = {"n": len(rows), "by_horizon": buckets,
                        "kept_rate": _rate(sum(p.status in _KEPT for p in rows), len(rows))}
    return out


def _paid_on(db: Session, p: PaymentPromise):
    """When the subject behind a promise was actually paid (date), or None."""
    if p.subject_type == "mandate_run":
        r = db.query(MandateRun).filter(MandateRun.id == p.subject_id).first()
        if r and r.status == MandateStatus.RECOVERED.value and r.closed_at:
            return r.closed_at.date()
    elif p.subject_type == "invoice":
        i = db.query(Invoice).filter(Invoice.id == p.subject_id).first()
        if i and i.status == InvoiceStatus.PAID.value and i.paid_at:
            return i.paid_at.date()
    return None


def promise_late_payment(db: Session, since: datetime | None) -> dict:
    """Of the promises marked BROKEN, how many were in fact paid shortly
    after the date - the sign that the grace period is too tight."""
    broken = [p for p in _resolved_promises(db, since) if p.status == PromiseStatus.BROKEN.value]
    buckets = {"paid_1d_after": 0, "paid_2d_after": 0, "paid_3_to_7d_after": 0,
               "paid_over_7d_after": 0, "never_paid": 0}
    for p in broken:
        paid = _paid_on(db, p)
        if paid is None:
            buckets["never_paid"] += 1
            continue
        late = (paid - p.promised_date).days
        if late <= 1:
            buckets["paid_1d_after"] += 1
        elif late == 2:
            buckets["paid_2d_after"] += 1
        elif late <= 7:
            buckets["paid_3_to_7d_after"] += 1
        else:
            buckets["paid_over_7d_after"] += 1
    soon = buckets["paid_1d_after"] + buckets["paid_2d_after"]
    return {"n": len(broken), "buckets": buckets, "paid_within_2d_after": soon,
            "share_paid_within_2d_after": _rate(soon, len(broken)),
            "current_grace_days": runtime_flags.get_promise_grace_days()}


def promise_repeat(db: Session, since: datetime | None) -> dict:
    """Kept rate by how many promises the same debt has already had."""
    by_subject: dict[tuple, list[PaymentPromise]] = {}
    for p in _resolved_promises(db, since):
        by_subject.setdefault((p.subject_type, p.subject_id), []).append(p)
    seq: dict[str, dict] = {"1st": {"promises": 0, "kept": 0}, "2nd": {"promises": 0, "kept": 0},
                            "3rd_or_later": {"promises": 0, "kept": 0}}
    for rows in by_subject.values():
        for i, p in enumerate(sorted(rows, key=lambda x: x.created_at)):
            key = "1st" if i == 0 else "2nd" if i == 1 else "3rd_or_later"
            seq[key]["promises"] += 1
            seq[key]["kept"] += p.status in _KEPT
    for v in seq.values():
        v["kept_rate"] = _rate(v["kept"], v["promises"])
    follow_ups = seq["2nd"]["promises"] + seq["3rd_or_later"]["promises"]
    return {"n": follow_ups, "by_sequence": seq,
            "current_max_broken": runtime_flags.get_promise_max_broken()}


# ── Voice ────────────────────────────────────────────────────────────────────

def _productive(call: VoiceCall) -> bool:
    r = call.routed_to or ""
    return r.startswith("payment_link_sent") or r.startswith("promise_created")


def voice_by_attempt(db: Session, since: datetime | None) -> dict:
    q = db.query(VoiceCall)
    if since:
        q = q.filter(VoiceCall.created_at >= since)
    by_subject: dict[tuple, list[VoiceCall]] = {}
    for c in q.all():
        by_subject.setdefault((c.subject_type, c.subject_id), []).append(c)
    attempts: dict[int, dict] = {}
    total = 0
    for calls in by_subject.values():
        for i, c in enumerate(sorted(calls, key=lambda x: x.created_at), start=1):
            a = attempts.setdefault(i, {"calls": 0, "productive": 0, "no_answer": 0})
            a["calls"] += 1
            a["productive"] += _productive(c)
            a["no_answer"] += c.status == "no_answer"
            total += 1
    ladder = [{"call_number": n, **a, "productive_rate": _rate(a["productive"], a["calls"])}
              for n, a in sorted(attempts.items())]
    return {"n": total, "ladder": ladder,
            "current_max_attempts": runtime_flags.get_voice_max_attempts_per_subject()}


# ── Receivables ──────────────────────────────────────────────────────────────

def receivables_timing(db: Session, since: datetime | None) -> dict:
    """For invoices that went overdue and were then paid: how many days past
    due did the money actually arrive?"""
    q = db.query(Invoice).filter(Invoice.status == InvoiceStatus.PAID.value,
                                 Invoice.paid_at.isnot(None), Invoice.overdue_booked.is_(True))
    if since:
        q = q.filter(Invoice.due_at >= since)
    paid = q.all()
    edges = [("0-7d", 7), ("8-15d", 15), ("16-30d", 30), ("31-60d", 60), ("61d+", float("inf"))]
    buckets = {label: {"invoices": 0, "amount_inr": 0.0} for label, _ in edges}
    after = 0
    esc = runtime_flags.get_receivables_escalate_after_days()
    for inv in paid:
        d = max(0.0, (inv.paid_at - inv.due_at).total_seconds() / 86400)
        b = buckets[_bucket(d, edges)]
        b["invoices"] += 1
        b["amount_inr"] = round(b["amount_inr"] + inv.amount_inr, 2)
        after += d > esc
    return {"n": len(paid), "by_days_past_due": buckets, "paid_after_escalation_day": after,
            "share_paid_after_escalation_day": _rate(after, len(paid)),
            "current_escalate_after_days": esc}


# ── Context metrics ──────────────────────────────────────────────────────────

MAX_COLLECTIONS_PATTERNS = 12     # more mechanisms than the other analyses, so more room (they use MAX_PATTERNS)
MIN_GROUP = 5          # a group smaller than this is shown but never turned into a pattern


def mandate_failure_reasons(db: Session, since: datetime | None) -> dict:
    """Recovery by WHY the debit failed. A retry can fix "insufficient funds" (money arrives) but not a cancelled mandate or an
    expired card, so the retry policy should not treat them alike. Settled runs only."""
    q = db.query(MandateRun).filter(MandateRun.risk_settled.is_(True))
    if since:
        q = q.filter(MandateRun.opened_at >= since)
    groups: dict[str, list[MandateRun]] = {}
    for r in q.all():
        groups.setdefault((r.failure_reason or "unknown").lower(), []).append(r)
    out = []
    for reason, runs in groups.items():
        rec = [r for r in runs if r.status == MandateStatus.RECOVERED.value]
        days = sorted((r.closed_at - r.opened_at).total_seconds() / 86400 for r in rec if r.closed_at)
        out.append({
            "reason": reason, "runs": len(runs), "recovered": len(rec), "recovery_rate": _rate(len(rec), len(runs)),
            "median_days_to_recover": round(median(days), 1) if days else None,
            "recovered_inr": round(sum(r.amount_inr for r in rec), 2),
            "lost_inr": round(sum(r.amount_inr for r in runs if r not in rec), 2),
        })
    out.sort(key=lambda g: -g["runs"])
    return {"n": sum(g["runs"] for g in out), "by_reason": out}


def receivables_outcomes(db: Session, since: datetime | None) -> dict:
    """Every invoice that went overdue, whatever became of it - receivables_timing only sees the ones that were paid, which flatters
    the picture. "Still open" is split by how long it has been overdue."""
    q = db.query(Invoice).filter(Invoice.overdue_booked.is_(True))
    if since:
        q = q.filter(Invoice.due_at >= since)
    invs = q.all()
    now = datetime.utcnow()
    count = {"paid": 0, "partially_paid": 0, "still_open": 0, "disputed": 0, "written_off": 0}
    amount = {k: 0.0 for k in count}
    open_age = {"0-30d": 0, "31-60d": 0, "61-90d": 0, "91d+": 0}
    for i in invs:
        key = {InvoiceStatus.PAID.value: "paid", InvoiceStatus.PARTIALLY_PAID.value: "partially_paid",
               InvoiceStatus.DISPUTED.value: "disputed", InvoiceStatus.WRITTEN_OFF.value: "written_off"}.get(i.status, "still_open")
        count[key] += 1
        amount[key] += i.amount_inr - (i.amount_paid if key == "partially_paid" else 0)
        if key == "still_open":
            open_age[_bucket(max(0.0, (now - i.due_at).total_seconds() / 86400),
                             [("0-30d", 30), ("31-60d", 60), ("61-90d", 90), ("91d+", float("inf"))])] += 1
    total_amount = sum(i.amount_inr for i in invs)
    paid_amount = sum(i.amount_paid for i in invs)
    return {"n": len(invs), "counts": count, "amount_inr": {k: round(v, 2) for k, v in amount.items()},
            "still_open_by_days_overdue": open_age,
            "paid_share": _rate(count["paid"], len(invs)),
            "collected_share_of_amount": _rate(int(paid_amount), int(total_amount)) if total_amount else None,
            "written_off_share": _rate(count["written_off"], len(invs))}


def chase_step_effectiveness(db: Session, since: datetime | None) -> dict:
    """Which reminder is followed by payment. For each paid invoice the LAST chase step that ran before the payment gets the credit when
    the payment came within 3 days of it (a payment long after a step is not that step's doing). Last-touch, so it understates a step that
    only prepares the ground."""
    from app.rules_engine import ESCALATE_STEP
    steps: dict[int, dict] = {}
    q = db.query(Invoice).filter(Invoice.overdue_booked.is_(True))
    if since:
        q = q.filter(Invoice.due_at >= since)
    for inv in q.all():
        rows = (db.query(ScheduledAction).filter(
            ScheduledAction.subject_id == inv.id, ScheduledAction.kind == ScheduledKind.INVOICE_CHASE.value,
            ScheduledAction.status == ScheduledStatus.DONE.value, ScheduledAction.executed_at.isnot(None))
            .order_by(ScheduledAction.executed_at.asc()).all())
        done = []
        for r in rows:
            try:
                done.append((int(json.loads(r.payload_json or "{}").get("step")), r.executed_at))
            except (TypeError, ValueError):
                continue
        for st, at in done:
            steps.setdefault(st, {"sent": 0, "paid_within_3d": 0})["sent"] += 1
        if inv.status == InvoiceStatus.PAID.value and inv.paid_at:
            before = [(st, at) for st, at in done if at <= inv.paid_at]
            if before:
                st, at = before[-1]
                if inv.paid_at - at <= timedelta(days=3):
                    steps[st]["paid_within_3d"] += 1
    out = [{"step": st, "name": "hand to finance" if st == ESCALATE_STEP else f"reminder {st + 1}", **v,
            "paid_rate": _rate(v["paid_within_3d"], v["sent"])} for st, v in sorted(steps.items())]
    return {"n": sum(v["sent"] for v in steps.values()), "steps": out}


def link_conversion(db: Session, since: datetime | None) -> dict:
    """Real payment links only (a stand-in cannot be paid). A link replaced by a newer one is not counted: it never had its chance."""
    q = db.query(PaymentLink).filter(PaymentLink.is_stub.is_(False),
                                     PaymentLink.status != PaymentLinkStatus.SUPERSEDED.value)
    if since:
        q = q.filter(PaymentLink.created_at >= since)
    by: dict[str, list[PaymentLink]] = {}
    for l in q.all():
        by.setdefault(l.subject_type, []).append(l)
    out = []
    for st, links in sorted(by.items()):
        paid = [l for l in links if l.status == PaymentLinkStatus.PAID.value]
        part = [l for l in links if l.status == PaymentLinkStatus.PARTIALLY_PAID.value]
        dead = [l for l in links if l.status in (PaymentLinkStatus.EXPIRED.value, PaymentLinkStatus.CANCELLED.value)
                and not l.amount_paid_inr]
        hours = sorted((l.closed_at - l.created_at).total_seconds() / 3600 for l in paid if l.closed_at)
        out.append({"subject_type": st, "links": len(links), "paid": len(paid), "partially_paid": len(part),
                    "expired_unpaid": len(dead), "still_open": len(links) - len(paid) - len(part) - len(dead),
                    "paid_rate": _rate(len(paid) + len(part), len(links)),
                    "median_hours_to_pay": round(median(hours), 1) if hours else None,
                    "amount_paid_inr": round(sum(l.amount_paid_inr or 0 for l in links), 2)})
    return {"n": sum(g["links"] for g in out), "by_subject": out}


def promises_by_source(db: Session, since: datetime | None) -> dict:
    groups: dict[str, list[PaymentPromise]] = {}
    for p in _resolved_promises(db, since):
        groups.setdefault(p.captured_via or "unknown", []).append(p)
    out = [{"source": src, "promises": len(rows), "kept": sum(p.status in _KEPT for p in rows),
            "kept_rate": _rate(sum(p.status in _KEPT for p in rows), len(rows))} for src, rows in groups.items()]
    out.sort(key=lambda g: -g["promises"])
    return {"n": sum(g["promises"] for g in out), "by_source": out}


def _call_end(call: VoiceCall) -> str:
    r = call.routed_to or ""
    if call.status == "no_answer":
        return "no_answer"
    if r.startswith("promise_created"):
        return "promise"
    if r.startswith("payment_link_sent"):
        return "payment_link"
    if r.startswith("callback"):
        return "callback"
    if r.startswith(("escalated", "handoff", "disput")) or "human" in r:
        return "handed_to_person"
    return "other"


def voice_outcomes(db: Session, since: datetime | None) -> dict:
    q = db.query(VoiceCall)
    if since:
        q = q.filter(VoiceCall.created_at >= since)
    calls = q.all()
    counts = {k: 0 for k in ("promise", "payment_link", "callback", "no_answer", "handed_to_person", "other")}
    for c in calls:
        counts[_call_end(c)] += 1
    n = len(calls)
    return {"n": n, "counts": counts, "shares": {k: _rate(v, n) for k, v in counts.items()}}


def trend(db: Session, since: datetime | None) -> dict | None:
    """This window against the equal window just before it. None for "all time" (nothing to compare with). Recent windows have more
    cases still open, and only settled cases count, so read a small change with care."""
    if since is None:
        return None
    now = datetime.utcnow()
    prev_since = since - (now - since)

    def runs(a, b):
        rs = db.query(MandateRun).filter(MandateRun.risk_settled.is_(True), MandateRun.opened_at >= a, MandateRun.opened_at < b).all()
        return len(rs), _rate(sum(r.status == MandateStatus.RECOVERED.value for r in rs), len(rs))

    def proms(a, b):
        ps = [p for p in db.query(PaymentPromise).filter(PaymentPromise.status.in_(_RESOLVED_PROMISE),
                                                         PaymentPromise.created_at >= a, PaymentPromise.created_at < b).all()]
        return len(ps), _rate(sum(p.status in _KEPT for p in ps), len(ps))

    def calls(a, b):
        cs = db.query(VoiceCall).filter(VoiceCall.created_at >= a, VoiceCall.created_at < b).all()
        return len(cs), _rate(sum(_productive(c) for c in cs), len(cs))

    out = {}
    for key, fn in (("mandate_recovery_rate", runs), ("promise_kept_rate", proms), ("voice_productive_rate", calls)):
        (n_now, r_now), (n_prev, r_prev) = fn(since, now + timedelta(days=1)), fn(prev_since, since)
        out[key] = {"now": r_now, "n_now": n_now, "before": r_prev, "n_before": n_prev}
    return out


# ── Evidence + patterns ──────────────────────────────────────────────────────

def evidence_status(m: dict, min_sample: int) -> dict:
    """param -> {metric, n, sufficient}. The router also uses this to drop any
    recommendation whose evidence is below the minimum sample."""
    n = {
        "mandate_max_retries": m["mandate_attempts"]["n"],
        "mandate_retry_window_days": m["mandate_timing"]["n"],
        "promise_max_days_b2c": m["promises_by_horizon"]["b2c"]["n"],
        "promise_max_days_b2b": m["promises_by_horizon"]["b2b"]["n"],
        "promise_grace_days": m["promise_late_payment"]["n"],
        "promise_max_broken": m["promise_repeat"]["n"],
        "voice_max_attempts_per_subject": m["voice_by_attempt"]["n"],
        "receivables_escalate_after_days": m["receivables_timing"]["n"],
    }
    return {p: {"metric": EVIDENCE_METRIC[p], "n": n[p], "min_sample": min_sample,
                "sufficient": n[p] >= min_sample} for p in n}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def generate_collections_patterns(m: dict, evidence: dict, cfg: dict) -> list[dict]:
    """Deterministic plain-English observations (wording controlled here so it
    survives the LLM being unreachable). Only ever states a pattern its own
    metric has enough data for."""
    patterns: list[dict] = []

    def add(kind: str, text: str):
        patterns.append({"kind": kind, "text": text})

    ev = evidence
    ma = m["mandate_attempts"]
    if ev["mandate_max_retries"]["sufficient"] and ma["ladder"]:
        cap = cfg["mandate_max_retries"]
        beyond = [r for r in ma["ladder"] if r["retry_number"] > 2 and r["runs_reaching"] > 0]
        dead = [r for r in beyond if r["recovered_here"] == 0]
        if beyond and len(dead) == len(beyond):
            first = beyond[0]["retry_number"]
            add("opportunity",
                f"No failed recurring debit has been recovered by retry {first} or later "
                f"({sum(r['runs_reaching'] for r in beyond)} runs reached them). "
                f"The cap is {cap} retries - the extra attempts are buying nothing.")
        elif ma["recovery_rate"] is not None:
            add("info", f"{_pct(ma['recovery_rate'])} of {ma['n']} settled recurring-debit runs "
                        f"were recovered.")

    mt = m["mandate_timing"]
    if ev["mandate_retry_window_days"]["sufficient"] and mt["recovered"]:
        add("info",
            f"Recovered recurring debits came back a median of {mt['median_days']} days after the "
            f"first failure (90% within {mt['p90_days']} days); the retry window is "
            f"{cfg['mandate_retry_window_days']} days.")

    for seg, key in (("consumer", "b2c"), ("business", "b2b")):
        ph = m["promises_by_horizon"][key]
        pk = f"promise_max_days_{key}"
        if ev[pk]["sufficient"]:
            far = [(lbl, b) for lbl, b in ph["by_horizon"].items()
                   if lbl in ("15-30d", "31d+") and b["promises"] >= 3]
            near = [b for lbl, b in ph["by_horizon"].items() if lbl in ("1-3d", "4-7d")]
            near_n = sum(b["promises"] for b in near)
            near_kept = sum(b["kept"] for b in near)
            far_n = sum(b["promises"] for _, b in far)
            far_kept = sum(b["kept"] for _, b in far)
            if near_n and far_n and _rate(far_kept, far_n) < _rate(near_kept, near_n):
                add("warning",
                    f"{seg.capitalize()} promises more than 14 days out are kept "
                    f"{_pct(_rate(far_kept, far_n))} of the time vs "
                    f"{_pct(_rate(near_kept, near_n))} for promises within a week "
                    f"(horizon limit is {cfg[pk]} days).")

    lp = m["promise_late_payment"]
    if ev["promise_grace_days"]["sufficient"] and (lp["share_paid_within_2d_after"] or 0) >= 0.25:
        add("opportunity",
            f"{lp['paid_within_2d_after']} of {lp['n']} broken promises "
            f"({_pct(lp['share_paid_within_2d_after'])}) were paid within 2 days of the date - "
            f"the {cfg['promise_grace_days']}-day grace period is marking them broken too early.")

    pr = m["promise_repeat"]["by_sequence"]
    if ev["promise_max_broken"]["sufficient"] and pr["1st"]["kept_rate"] is not None \
            and pr["2nd"]["promises"] >= 3 and pr["2nd"]["kept_rate"] is not None \
            and pr["2nd"]["kept_rate"] < pr["1st"]["kept_rate"]:
        add("warning",
            f"Second promises on the same debt are kept {_pct(pr['2nd']['kept_rate'])} of the time "
            f"vs {_pct(pr['1st']['kept_rate'])} for first promises.")

    vc = m["voice_by_attempt"]["ladder"]
    if ev["voice_max_attempts_per_subject"]["sufficient"] and len(vc) >= 2:
        first, last = vc[0], vc[-1]
        if first["productive_rate"] is not None and last["productive_rate"] is not None \
                and last["calls"] >= 3 and last["productive_rate"] < first["productive_rate"] / 2:
            add("opportunity",
                f"Call {last['call_number']} is productive {_pct(last['productive_rate'])} of the time "
                f"vs {_pct(first['productive_rate'])} for call 1 "
                f"(cap is {cfg['voice_max_attempts_per_subject']} calls per debt).")

    rt = m["receivables_timing"]
    if ev["receivables_escalate_after_days"]["sufficient"] and rt["share_paid_after_escalation_day"] is not None:
        add("info",
            f"{_pct(rt['share_paid_after_escalation_day'])} of overdue invoices that were eventually "
            f"paid arrived after day {cfg['receivables_escalate_after_days']} (the escalation day).")

    # ── context patterns: each states only what its own group has at least MIN_GROUP cases for ──
    fr = [g for g in m["mandate_failure_reasons"]["by_reason"] if g["runs"] >= MIN_GROUP]
    if len(fr) >= 2:
        best = max(fr, key=lambda g: g["recovery_rate"] or 0)
        worst = min(fr, key=lambda g: g["recovery_rate"] or 0)
        if (best["recovery_rate"] or 0) - (worst["recovery_rate"] or 0) >= 0.3:
            add("opportunity",
                f"Debits that failed for \"{worst['reason'].replace('_', ' ')}\" are recovered {_pct(worst['recovery_rate'])} of the time "
                f"({worst['runs']} runs) vs {_pct(best['recovery_rate'])} for \"{best['reason'].replace('_', ' ')}\" ({best['runs']} runs) - "
                f"retrying them the same way is not worth it.")

    ro = m["receivables_outcomes"]
    if ro["n"] >= MIN_GROUP:
        c, amt = ro["counts"], ro["amount_inr"]
        stuck = c["still_open"] + c["disputed"]
        old = ro["still_open_by_days_overdue"]["61-90d"] + ro["still_open_by_days_overdue"]["91d+"]
        add("warning" if old else "info",
            f"Of {ro['n']} overdue invoices, {c['paid']} are paid ({_pct(ro['paid_share'])}), {c['still_open']} still open "
            f"(₹{amt['still_open']:,.0f}; {old} of them more than 60 days overdue), {c['disputed']} disputed and "
            f"{c['written_off']} written off." + (" Invoices that were never paid are counted here, unlike the timing figure above." if stuck else ""))

    ce = [s_ for s_ in m["chase_step_effectiveness"]["steps"] if s_["sent"] >= MIN_GROUP]
    if len(ce) >= 2:
        top = max(ce, key=lambda s_: s_["paid_rate"] or 0)
        low = min(ce, key=lambda s_: s_["paid_rate"] or 0)
        if top["step"] != low["step"] and (top["paid_rate"] or 0) - (low["paid_rate"] or 0) >= 0.15:
            add("info",
                f"Payment follows {top['name']} within 3 days {_pct(top['paid_rate'])} of the time ({top['sent']} sent) vs "
                f"{_pct(low['paid_rate'])} for {low['name']} ({low['sent']} sent) - the chase dates are set in the receivables settings.")

    for g in m["link_conversion"]["by_subject"]:
        if g["links"] >= MIN_GROUP:
            label = {"invoice": "invoice", "mandate_run": "failed-debit", "order": "failed-order"}.get(g["subject_type"], g["subject_type"])
            tail = f"; payers took a median {g['median_hours_to_pay']} hours" if g["median_hours_to_pay"] is not None else ""
            add("info", f"{_pct(g['paid_rate'])} of {g['links']} real {label} payment links were paid and {g['expired_unpaid']} "
                        f"expired unpaid{tail}.")

    src = [g for g in m["promises_by_source"]["by_source"] if g["promises"] >= MIN_GROUP]
    if len(src) >= 2:
        top = max(src, key=lambda g: g["kept_rate"] or 0)
        low = min(src, key=lambda g: g["kept_rate"] or 0)
        if (top["kept_rate"] or 0) - (low["kept_rate"] or 0) >= 0.2:
            nice = lambda x: x.replace("_", " ")
            add("info", f"Promises made through {nice(top['source'])} are kept {_pct(top['kept_rate'])} of the time "
                        f"({top['promises']}) vs {_pct(low['kept_rate'])} through {nice(low['source'])} ({low['promises']}).")

    vo = m["voice_outcomes"]
    if vo["n"] >= MIN_GROUP and (vo["shares"]["handed_to_person"] or 0) + (vo["shares"]["no_answer"] or 0) >= 0.3:
        add("info", f"{_pct(vo['shares']['no_answer'])} of {vo['n']} calls went unanswered and "
                    f"{_pct(vo['shares']['handed_to_person'])} ended with a hand-off to a person.")

    tr = m["trend"]
    if tr:
        for key, label in (("mandate_recovery_rate", "recurring debits recovered"), ("promise_kept_rate", "promises kept"),
                           ("voice_productive_rate", "calls that were productive")):
            t = tr[key]
            if t["n_now"] >= MIN_GROUP and t["n_before"] >= MIN_GROUP and t["now"] is not None and t["before"] is not None \
                    and abs(t["now"] - t["before"]) >= 0.1:
                better = t["now"] > t["before"]
                add("info", f"Share of {label}: {_pct(t['now'])} now vs {_pct(t['before'])} in the window before "
                            f"({'up' if better else 'down'}; {t['n_now']} vs {t['n_before']} cases).")

    short = [p for p, e in ev.items() if not e["sufficient"]]
    if short:
        n_min = next(iter(ev.values()))["min_sample"]
        add("info",
            f"Not enough resolved cases yet for {len(short)} of {len(ev)} settings "
            f"(each needs at least {n_min}). They will not be recommended on until then.")

    patterns.sort(key=lambda p: _PATTERN_PRIORITY[p["kind"]])
    return patterns[:MAX_COLLECTIONS_PATTERNS]


def build_analysis_input(db: Session, range_key: str) -> dict:
    since, normalized_range = resolve_range(range_key)
    min_sample = runtime_flags.get_collections_min_sample()
    metrics = {
        "mandate_attempts": mandate_attempts(db, since),
        "mandate_timing": mandate_timing(db, since),
        "promises_by_horizon": promises_by_horizon(db, since),
        "promise_late_payment": promise_late_payment(db, since),
        "promise_repeat": promise_repeat(db, since),
        "voice_by_attempt": voice_by_attempt(db, since),
        "receivables_timing": receivables_timing(db, since),
    }
    context = {
        "mandate_failure_reasons": mandate_failure_reasons(db, since),
        "receivables_outcomes": receivables_outcomes(db, since),
        "chase_step_effectiveness": chase_step_effectiveness(db, since),
        "link_conversion": link_conversion(db, since),
        "promises_by_source": promises_by_source(db, since),
        "voice_outcomes": voice_outcomes(db, since),
        "trend": trend(db, since),
    }
    cfg = config_snapshot()
    evidence = evidence_status(metrics, min_sample)
    return {
        "range": normalized_range,
        "since": since.isoformat() if since else None,
        "generated_at": datetime.utcnow().isoformat(),
        "min_sample": min_sample,
        "config_snapshot": cfg,
        **metrics,
        **context,
        "evidence": evidence,
        "patterns": generate_collections_patterns({**metrics, **context}, evidence, cfg),
    }

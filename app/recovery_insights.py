"""
Pure aggregation layer for the whole-funnel loss & recovery analysis -
same "deterministic layer, no LLM, no writes" separation as app/insights.py.
See recovery_insights_llm.py for the recommendation layer and
insights_router.py for the endpoint that wires the two together.

Covers every way this system loses revenue, in one comparable shape,
rather than the payment-failure pipeline alone (which is what this module
used to be, back when it was failure_insights.py):

- silent_abandon   cart went idle             (CartEvent)
- explicit_cancel  customer deleted the cart  (CartEvent)
- payment_failure  gateway declined           (Order runs, see _payment_runs)
- give-up          customer walked away from a failed payment
                   (a RESOLUTION of a payment_failure run, not a separate
                   leak - its money is already inside that row's "lost")

Double-counting is the thing to be careful about here, because the threads
hand off to each other: a cart offer that gets RESUMED and then fails at
the gateway has its money moved out of the cart bucket and into
at_risk_failed by revenue.resolve_cart_to_failed_thread. That case is
reported as its own outcome ("handed to checkout, payment failed") so the
same rupees are never counted as both a cart loss and a payment loss.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app import runtime_flags
from app.insights import (  # noqa: F401 (RANGE_PRESETS re-exported)
    RANGE_PRESETS, resolve_range, LOW_SAMPLE_THRESHOLD, MAX_PATTERNS, _PATTERN_PRIORITY, _inr,
    _converted,
)
from app.models import (
    AuditLog, CartEvent, CartEventStatus, CartEventType, Customer, Decision, Event, EventType,
    Order, OrderStatus, PendingSignal, PendingSignalKind,
)

# Matches the exact text webhook.py/dropoff.py generate on a gate override -
# both use the identical format, so one pattern covers both pipelines.
import re
_OVERRIDE_ACTION_RE = re.compile(r"Original decision was '(\w+)'")
_HIGH_VALUE_MARKER = "above the high-value threshold"

# The source string llm_agent.py actually writes on a successful LLM call.
# This used to be compared against a bare "llm", which never matched
# anything - so "handled by the real AI" always read 0 and the AI-vs-rules
# escalation comparison was always null, no matter how much of the system
# was really AI-driven.
_LLM_SOURCE = "llm_agent"
_FALLBACK_SOURCE = "rules_engine_fallback"

SIGNAL_LABELS = {
    "silent_abandon": "Cart went quiet",
    "explicit_cancel": "Cart deleted",
    "payment_failure": "Payment failed",
}


def config_snapshot() -> dict:
    """The whitelist this analysis is allowed to touch."""
    return {
        "confidence_threshold": runtime_flags.get_confidence_threshold(),
        "max_auto_retries": runtime_flags.get_max_auto_retries(),
        "high_value_amount_inr": runtime_flags.get_high_value_amount_inr(),
    }


def _events(db: Session, event_type: EventType, since: datetime | None) -> list[Event]:
    q = db.query(Event).filter(Event.event_type == event_type)
    if since:
        q = q.filter(Event.received_at >= since)
    return q.all()


def _item_signature(items_json: str | None):
    """Order-independent (sku, qty) key - the same normalization used to
    group retries of one purchase everywhere else in the codebase."""
    try:
        items = json.loads(items_json or "[]")
        return tuple(sorted((i.get("sku"), i.get("qty")) for i in items))
    except (TypeError, ValueError, AttributeError):
        return None


def _given_up_order_ids(db: Session) -> set:
    """Carrier order ids the customer explicitly gave up on, read from the
    payment_failure_given_up audit entries - the only place give-up is
    distinguishable from a silent lapse (both end with risk_settled=True
    on the carrier, so the Order row alone can't tell them apart).

    Deliberately not range-filtered: a run that started failing inside the
    window may have been given up after it.
    """
    ids = set()
    for entry in db.query(AuditLog).filter(AuditLog.action_type == "payment_failure_given_up").all():
        try:
            details = json.loads(entry.details)
        except (TypeError, ValueError):
            continue
        for run in details.get("settled_runs") or []:
            rid = run.get("razorpay_order_id")
            if rid:
                ids.add(rid)
    return ids


def _payment_runs(db: Session, since: datetime | None) -> list[dict]:
    """Every failed-payment RUN that started in the window - the single
    source of truth every payment-side metric below derives from, so they
    can't drift apart the way three separate ad-hoc groupings did before.

    A run is one purchase attempt-chain: all orders for the same customer
    and the same basket, from the first failure onward. /checkout mints a
    new Order per retry, so counting Order rows would count one purchase
    several times.

    A CAPTURED order still carrying a failure_reason is a same-order
    retry that eventually worked (webhook.py never clears failure_reason
    on recapture) - included, or a run recovered inside the Razorpay modal
    would be invisible here and recovery rates would read low.
    """
    q = db.query(Order).filter(
        (Order.status == OrderStatus.FAILED)
        | ((Order.status == OrderStatus.CAPTURED) & (Order.failure_reason.isnot(None)))
    )
    if since:
        q = q.filter(Order.created_at >= since)
    seed_orders = q.all()
    if not seed_orders:
        return []

    customer_ids = {o.customer_id for o in seed_orders}
    # Unscoped by date on purpose: a capture just after the window still
    # resolved the run, and pretending otherwise would understate recovery.
    all_orders = db.query(Order).filter(Order.customer_id.in_(customer_ids)).all()
    by_key: dict[tuple, list[Order]] = {}
    for o in all_orders:
        sig = _item_signature(o.items_json)
        if sig is None:
            continue
        by_key.setdefault((o.customer_id, sig), []).append(o)

    given_up_ids = _given_up_order_ids(db)
    tiers = {
        c.id: c.tier.value
        for c in db.query(Customer).filter(Customer.id.in_(customer_ids)).all()
    }

    runs = []
    seen_keys = set()
    for seed in seed_orders:
        sig = _item_signature(seed.items_json)
        if sig is None:
            continue
        key = (seed.customer_id, sig)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        group = sorted(by_key.get(key, []), key=lambda o: o.created_at)
        failures = [
            o for o in group
            if o.status == OrderStatus.FAILED
            or (o.status == OrderStatus.CAPTURED and o.failure_reason is not None)
        ]
        if not failures:
            continue
        carrier = failures[0]

        after_carrier = [o for o in group if o.created_at >= carrier.created_at]
        captured = [o for o in after_carrier if o.status == OrderStatus.CAPTURED]
        recovered = bool(captured)

        runs.append({
            "customer_id": carrier.customer_id,
            "tier": tiers.get(carrier.customer_id, "unknown"),
            "carrier_order_id": carrier.razorpay_order_id,
            "amount_inr": carrier.amount_inr,
            "failure_reason": carrier.failure_reason.value if carrier.failure_reason else "unknown",
            "attempts": len(after_carrier),
            "recovered": recovered,
            "recovered_amount_inr": round(sum(o.amount_inr for o in captured), 2) if recovered else 0.0,
            # risk_settled on the carrier is what the real ledger uses to
            # mean "this run's at-risk money has been closed out".
            "still_open": (not recovered) and (not carrier.risk_settled),
            "lost": (not recovered) and bool(carrier.risk_settled),
            "gave_up": carrier.razorpay_order_id in given_up_ids,
        })
    return runs


def _cart_events(db: Session, event_type: CartEventType, since: datetime | None) -> list[CartEvent]:
    q = db.query(CartEvent).filter(CartEvent.event_type == event_type)
    if since:
        q = q.filter(CartEvent.created_at >= since)
    return q.all()


def _cart_outcome(db: Session, ce: CartEvent) -> str:
    """recovered | handed_to_checkout | still_open | lost - mutually
    exclusive, checked in that order.

    handed_to_checkout is the case that keeps this honest: the customer
    came back and checked out, but the payment then failed, so the money
    moved to the failed-payment thread (revenue.resolve_cart_to_failed_thread)
    and is counted in THAT row. Without this it would be double-counted as
    both a cart loss and a payment loss.
    """
    if _converted(db, ce.id):
        return "recovered"

    failed_attributed = db.query(Order.id).filter(
        Order.recovered_from_cart_event_id == ce.id,
        Order.status == OrderStatus.FAILED,
    ).first()
    if failed_attributed:
        return "handed_to_checkout"

    if ce.status == CartEventStatus.PENDING:
        return "still_open"
    if ce.status is None:
        live_signal = db.query(PendingSignal.id).filter(
            PendingSignal.cart_event_id == ce.id,
            PendingSignal.kind == PendingSignalKind.TIMEOUT_ATTRIBUTION,
            PendingSignal.consumed_at.is_(None),
            PendingSignal.expires_at > datetime.utcnow(),
        ).first()
        if live_signal:
            return "still_open"
    return "lost"


def _recovered_amount_for_cart_event(db: Session, ce: CartEvent) -> float:
    captured = db.query(Order.amount_inr).filter(
        Order.recovered_from_cart_event_id == ce.id,
        Order.status == OrderStatus.CAPTURED,
    ).scalar()
    return captured or 0.0


def leak_summary(db: Session, since: datetime | None, runs: list[dict]) -> dict:
    """One row per signal, same columns for each - which is the whole point:
    a merchant can only judge where to spend recovery effort if the three
    leaks are described the same way."""
    rows = []

    for event_type in (CartEventType.SILENT_ABANDON, CartEventType.EXPLICIT_CANCEL):
        events = _cart_events(db, event_type, since)
        row = {
            "signal": event_type.value,
            "label": SIGNAL_LABELS[event_type.value],
            "count": len(events),
            "amount_at_stake_inr": round(sum(e.amount_inr for e in events), 2),
            "recovered_count": 0, "recovered_inr": 0.0,
            "still_open_count": 0, "still_open_inr": 0.0,
            "lost_count": 0, "lost_inr": 0.0,
            "handed_to_checkout_count": 0, "handed_to_checkout_inr": 0.0,
        }
        for e in events:
            outcome = _cart_outcome(db, e)
            if outcome == "recovered":
                row["recovered_count"] += 1
                row["recovered_inr"] += _recovered_amount_for_cart_event(db, e)
            elif outcome == "handed_to_checkout":
                row["handed_to_checkout_count"] += 1
                row["handed_to_checkout_inr"] += e.amount_inr
            elif outcome == "still_open":
                row["still_open_count"] += 1
                row["still_open_inr"] += e.amount_inr
            else:
                row["lost_count"] += 1
                row["lost_inr"] += e.amount_inr
        rows.append(row)

    payment_row = {
        "signal": "payment_failure",
        "label": SIGNAL_LABELS["payment_failure"],
        "count": len(runs),
        "amount_at_stake_inr": round(sum(r["amount_inr"] for r in runs), 2),
        "recovered_count": len([r for r in runs if r["recovered"]]),
        "recovered_inr": round(sum(r["recovered_amount_inr"] for r in runs), 2),
        "still_open_count": len([r for r in runs if r["still_open"]]),
        "still_open_inr": round(sum(r["amount_inr"] for r in runs if r["still_open"]), 2),
        "lost_count": len([r for r in runs if r["lost"]]),
        "lost_inr": round(sum(r["amount_inr"] for r in runs if r["lost"]), 2),
        # Nothing hands off out of the payment thread - it's the end of the line.
        "handed_to_checkout_count": 0, "handed_to_checkout_inr": 0.0,
    }
    rows.append(payment_row)

    for row in rows:
        for key in ("recovered_inr", "still_open_inr", "lost_inr", "handed_to_checkout_inr"):
            row[key] = round(row[key], 2)
        resolved = row["recovered_count"] + row["lost_count"]
        row["recovery_rate_pct"] = round(100 * row["recovered_count"] / resolved, 1) if resolved else None

    totals = {
        "count": sum(r["count"] for r in rows),
        "amount_at_stake_inr": round(sum(r["amount_at_stake_inr"] for r in rows), 2),
        "recovered_inr": round(sum(r["recovered_inr"] for r in rows), 2),
        "still_open_inr": round(sum(r["still_open_inr"] for r in rows), 2),
        "lost_inr": round(sum(r["lost_inr"] for r in rows), 2),
    }
    return {"signals": rows, "totals": totals}


def failure_reason_analysis(runs: list[dict]) -> list[dict]:
    """Per failure reason: how often, how recoverable, how much it cost -
    sorted by money lost, because that's the order a merchant would fix
    them in. Answers "which failures are worth retrying at all"."""
    by_reason: dict[str, dict] = {}
    for r in runs:
        agg = by_reason.setdefault(r["failure_reason"], {
            "reason": r["failure_reason"], "count": 0, "recovered": 0, "lost": 0,
            "lost_inr": 0.0, "still_open": 0,
        })
        agg["count"] += 1
        if r["recovered"]:
            agg["recovered"] += 1
        elif r["lost"]:
            agg["lost"] += 1
            agg["lost_inr"] += r["amount_inr"]
        else:
            agg["still_open"] += 1

    result = []
    for agg in by_reason.values():
        resolved = agg["recovered"] + agg["lost"]
        result.append({
            **agg,
            "lost_inr": round(agg["lost_inr"], 2),
            "recovery_rate_pct": round(100 * agg["recovered"] / resolved, 1) if resolved else None,
            "low_sample": agg["count"] < LOW_SAMPLE_THRESHOLD,
        })
    result.sort(key=lambda row: row["lost_inr"], reverse=True)
    return result


def retry_effectiveness(runs: list[dict]) -> dict:
    """Of the runs that reached attempt N, how many ever recovered? The
    direct evidence for whether max_auto_retries is too generous or too
    tight - if nothing recovers past attempt 2, allowing 3 just burns
    attempts (and patience)."""
    max_attempts = max((r["attempts"] for r in runs), default=0)
    ladder = []
    for n in range(1, max_attempts + 1):
        reached = [r for r in runs if r["attempts"] >= n]
        # "recovered at n or later" - reaching attempt n and eventually working
        recovered = [r for r in reached if r["recovered"]]
        ladder.append({
            "attempt": n,
            "runs_reaching": len(reached),
            "recovered": len(recovered),
            "recovery_rate_pct": round(100 * len(recovered) / len(reached), 1) if reached else None,
        })
    return {
        "ladder": ladder,
        "max_auto_retries": runtime_flags.get_max_auto_retries(),
        "avg_attempts": round(sum(r["attempts"] for r in runs) / len(runs), 2) if runs else None,
    }


def give_up_analysis(runs: list[dict]) -> dict:
    """Give-up is a RESOLUTION of a payment-failure run, not a separate
    leak - the money is already inside the payment row's "lost". What's
    worth knowing is the behaviour: do customers tell us they're done, or
    just vanish, and how hard did they try first?"""
    gave_up = [r for r in runs if r["gave_up"]]
    lost = [r for r in runs if r["lost"]]

    by_reason: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for r in gave_up:
        by_reason[r["failure_reason"]] = by_reason.get(r["failure_reason"], 0) + 1
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1

    return {
        "count": len(gave_up),
        "amount_inr": round(sum(r["amount_inr"] for r in gave_up), 2),
        "avg_attempts_before_giving_up": (
            round(sum(r["attempts"] for r in gave_up) / len(gave_up), 2) if gave_up else None
        ),
        "by_reason": by_reason,
        "by_tier": by_tier,
        # The rest of the losses just lapsed after the recovery window with
        # no word from the customer.
        "lapsed_silently_count": len(lost) - len([r for r in gave_up if r["lost"]]),
        "share_of_losses_pct": round(100 * len(gave_up) / len(lost), 1) if lost else None,
    }


def repeat_offenders_by_tier(runs: list[dict]) -> dict:
    """Customers with more than one distinct failed run in this window,
    by tier - are chronic failures actually landing in RISK, or slipping
    through?"""
    per_customer: dict[str, int] = {}
    tier_of: dict[str, str] = {}
    for r in runs:
        per_customer[r["customer_id"]] = per_customer.get(r["customer_id"], 0) + 1
        tier_of[r["customer_id"]] = r["tier"]

    by_tier: dict[str, int] = {}
    for cid, n in per_customer.items():
        if n > 1:
            tier = tier_of[cid]
            by_tier[tier] = by_tier.get(tier, 0) + 1
    return by_tier


def confidence_override_rates(db: Session, since: datetime | None) -> dict:
    """Escalation-by-confidence-gate rate, per pipeline, plus which ORIGINAL
    action most often gets overridden (parsed from the decision's own
    reasoning text - both gates write it in a fixed, self-consistent
    format, so this is a reliable extraction, not a guess)."""
    result = {}
    for label, event_type in (("payment_failure", EventType.PAYMENT_FAILED),
                               ("dropoff", EventType.CHECKOUT_ABANDONED)):
        events = _events(db, event_type, since)
        event_ids = [e.id for e in events]
        decisions = db.query(Decision).filter(Decision.event_id.in_(event_ids)).all() if event_ids else []

        total = len(decisions)
        escalated = [d for d in decisions if d.escalated]

        by_original_action: dict[str, int] = {}
        for d in escalated:
            m = _OVERRIDE_ACTION_RE.search(d.reasoning or "")
            key = m.group(1) if m else "unknown"
            by_original_action[key] = by_original_action.get(key, 0) + 1

        result[label] = {
            "total_decisions": total,
            "escalated_by_gate": len(escalated),
            "escalated_rate_pct": round(100 * len(escalated) / total, 1) if total else None,
            "escalated_by_original_action": by_original_action,
        }
    return result


def escalation_amount_analysis(db: Session, since: datetime | None) -> dict:
    """Of payment-failure escalations, how many were triggered PURELY by
    crossing high_value_amount_inr (nothing else was wrong) vs some other
    reason (risk, attempt cap, confidence gate)? Tells you if the floor is
    catching the right slice."""
    events = _events(db, EventType.PAYMENT_FAILED, since)
    event_ids = [e.id for e in events]
    event_by_id = {e.id: e for e in events}
    decisions = db.query(Decision).filter(
        Decision.event_id.in_(event_ids), Decision.action == "escalate_to_human",
    ).all() if event_ids else []

    purely_amount = 0
    other_reason = 0
    amounts = []
    threshold = runtime_flags.get_high_value_amount_inr()
    for d in decisions:
        ev = event_by_id.get(d.event_id)
        amounts.append(ev.amount_inr if ev else None)
        if _HIGH_VALUE_MARKER in (d.reasoning or ""):
            purely_amount += 1
        else:
            other_reason += 1

    near_floor = len([a for a in amounts if a is not None and threshold <= a < threshold * 1.2])

    return {
        "total_escalations": len(decisions),
        "purely_amount_triggered": purely_amount,
        "other_reason": other_reason,
        "near_floor_count": near_floor,  # escalated within 20% above the threshold
        "current_threshold_inr": threshold,
    }


def _decisions_in_range(db: Session, since: datetime | None) -> list[Decision]:
    all_events = (
        _events(db, EventType.PAYMENT_FAILED, since)
        + _events(db, EventType.CHECKOUT_ABANDONED, since)
    )
    event_ids = [e.id for e in all_events]
    return db.query(Decision).filter(Decision.event_id.in_(event_ids)).all() if event_ids else []


def agent_reliability(db: Session, since: datetime | None) -> dict:
    """How often the real Groq call actually decided vs falling back to the
    plain rulebook, and whether the two escalate at different rates.

    Compares against _LLM_SOURCE ("llm_agent") - the value llm_agent.py
    really writes. This was comparing against a bare "llm", which matched
    nothing, so every number here read 0/null regardless of reality.
    """
    decisions = _decisions_in_range(db, since)
    total = len(decisions)
    llm = [d for d in decisions if d.source == _LLM_SOURCE]
    fallback = [d for d in decisions if d.source == _FALLBACK_SOURCE]

    def escalation_rate(subset):
        if not subset:
            return None
        return round(100 * len([d for d in subset if d.escalated]) / len(subset), 1)

    return {
        "total_decisions": total,
        "llm_decisions": len(llm),
        "fallback_decisions": len(fallback),
        "fallback_rate_pct": round(100 * len(fallback) / total, 1) if total else None,
        "llm_escalation_rate_pct": escalation_rate(llm),
        "rules_fallback_escalation_rate_pct": escalation_rate(fallback),
    }


def generate_recovery_patterns(leak: dict, reasons: list[dict], give_up: dict,
                                retry: dict, escalation: dict, agent: dict) -> list[dict]:
    """Plain-English observations from the numbers above - same synthesis
    layer, shape and priority ordering as insights.py::generate_patterns.
    Pure derivation, no LLM, so it works even when Groq is unreachable."""
    patterns: list[dict] = []
    signals = leak["signals"]
    totals = leak["totals"]

    if totals["count"] == 0:
        return [{
            "kind": "info",
            "text": "No abandoned carts, cancellations or failed payments in this range - "
                     "nothing to analyse yet.",
        }]

    # 1. Biggest leak by money actually lost.
    worst = max(signals, key=lambda s: s["lost_inr"])
    if worst["lost_inr"] > 0:
        patterns.append({
            "kind": "warning",
            "text": f"Your biggest source of lost revenue is \"{worst['label'].lower()}\" - "
                    f"₹{_inr(worst['lost_inr'])} across {worst['lost_count']} of them.",
        })

    # 2/3. Best and worst recovering signal, among those with resolved volume.
    resolvable = [s for s in signals if s["recovery_rate_pct"] is not None
                  and (s["recovered_count"] + s["lost_count"]) >= 3]
    if resolvable:
        best = max(resolvable, key=lambda s: s["recovery_rate_pct"])
        if best["recovery_rate_pct"] > 0:
            patterns.append({
                "kind": "positive",
                "text": f"\"{best['label']}\" recovers best - {best['recovery_rate_pct']}% of them "
                        f"come back, worth ₹{_inr(best['recovered_inr'])} this period.",
            })
        if len(resolvable) >= 2:
            poor = min(resolvable, key=lambda s: s["recovery_rate_pct"])
            if poor["signal"] != best["signal"] and poor["recovery_rate_pct"] < 20:
                patterns.append({
                    "kind": "opportunity",
                    "text": f"\"{poor['label']}\" almost never comes back "
                            f"({poor['recovery_rate_pct']}% of {poor['recovered_count'] + poor['lost_count']}) - "
                            f"worth deciding whether chasing these is worth the effort at all.",
                })

    # 4. A failure reason that never recovers - retrying it is wasted effort.
    for row in reasons:
        resolved = row["recovered"] + row["lost"]
        if resolved >= 3 and row["recovered"] == 0 and row["lost_inr"] > 0:
            patterns.append({
                "kind": "opportunity",
                "text": f"Payments failing with \"{row['reason'].replace('_', ' ')}\" never recover "
                        f"(0 of {resolved}, ₹{_inr(row['lost_inr'])} lost) - retrying these doesn't "
                        f"help; they need the customer to fix something first.",
            })
            break

    # 5. Retry cliff - is max_auto_retries buying anything?
    ladder = retry.get("ladder") or []
    cap = retry.get("max_auto_retries")
    if cap and len(ladder) >= 2:
        beyond = [step for step in ladder if step["attempt"] > 1 and step["runs_reaching"] >= 3]
        dead = [step for step in beyond if step["recovered"] == 0]
        if dead:
            first_dead = dead[0]["attempt"]
            patterns.append({
                "kind": "opportunity",
                "text": f"No payment has ever recovered once it reached attempt {first_dead}, but the "
                        f"system allows up to {cap} tries - lowering that would stop burning "
                        f"attempts that don't work.",
            })

    # 6. Give-up behaviour.
    if give_up["count"] > 0:
        avg = give_up["avg_attempts_before_giving_up"]
        patterns.append({
            "kind": "info",
            "text": f"{give_up['count']} customer{'s' if give_up['count'] != 1 else ''} explicitly gave "
                    f"up on a payment (₹{_inr(give_up['amount_inr'])}), after "
                    f"{avg} attempt{'s' if avg != 1 else ''} on average.",
        })
    elif give_up["lapsed_silently_count"] > 0:
        patterns.append({
            "kind": "info",
            "text": f"{give_up['lapsed_silently_count']} failed payment"
                    f"{'s' if give_up['lapsed_silently_count'] != 1 else ''} were written off after the "
                    f"recovery window passed - the customers never came back or said anything.",
        })

    # 7. Escalation floor doing more work than it should.
    if escalation["total_escalations"] >= 3 and escalation["purely_amount_triggered"] > 0:
        share = 100 * escalation["purely_amount_triggered"] / escalation["total_escalations"]
        if share >= 50:
            patterns.append({
                "kind": "opportunity",
                "text": f"{escalation['purely_amount_triggered']} of {escalation['total_escalations']} "
                        f"handoffs to a human happened only because the amount crossed "
                        f"₹{_inr(escalation['current_threshold_inr'])} - nothing else was wrong. "
                        f"Raising that cutoff would cut manual review.",
            })

    # 8. AI actually running, or quietly falling back?
    if agent["total_decisions"] >= 3 and (agent["fallback_rate_pct"] or 0) >= 30:
        patterns.append({
            "kind": "warning",
            "text": f"The AI call failed and fell back to the plain rulebook "
                    f"{agent['fallback_rate_pct']}% of the time - decisions are still being made, "
                    f"but with less judgement than you'd expect.",
        })

    # 9. Money still in play - not lost, just not resolved.
    if totals["still_open_inr"] > 0:
        patterns.append({
            "kind": "info",
            "text": f"₹{_inr(totals['still_open_inr'])} is still in play - not lost yet, waiting on a "
                    f"retry or an offer that hasn't expired.",
        })

    # 10. Low sample, lowest priority.
    if totals["count"] < LOW_SAMPLE_THRESHOLD:
        patterns.append({
            "kind": "info",
            "text": f"Only {totals['count']} loss events in this range - not enough yet to draw firm "
                     f"conclusions.",
        })

    patterns.sort(key=lambda p: _PATTERN_PRIORITY[p["kind"]])
    return patterns[:MAX_PATTERNS]


def build_analysis_input(db: Session, range_key: str) -> dict:
    since, normalized_range = resolve_range(range_key)
    runs = _payment_runs(db, since)

    leak = leak_summary(db, since, runs)
    reasons = failure_reason_analysis(runs)
    give_up = give_up_analysis(runs)
    retry = retry_effectiveness(runs)
    escalation = escalation_amount_analysis(db, since)
    agent = agent_reliability(db, since)

    return {
        "range": normalized_range,
        "since": since.isoformat() if since else None,
        "generated_at": datetime.utcnow().isoformat(),
        "config_snapshot": config_snapshot(),
        "leak_summary": leak,
        "failure_reason_analysis": reasons,
        "give_up_analysis": give_up,
        "retry_effectiveness": retry,
        "escalation_amount_analysis": escalation,
        "confidence_override_rates": confidence_override_rates(db, since),
        "agent_reliability": agent,
        "repeat_offenders_by_tier": repeat_offenders_by_tier(runs),
        "patterns": generate_recovery_patterns(leak, reasons, give_up, retry, escalation, agent),
    }

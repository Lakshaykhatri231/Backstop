"""
Do the evals actually catch breakage? Each mutation below breaks the app in one specific way (in memory, never on disk) and the
named suite MUST then fail. A suite that stays green on broken code is worthless, so this runs before the baseline is trusted.

    python -m evals.selftest
"""
import importlib
import sys

from evals import harness


def _patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, old)


def mutations():
    from app import contacts, demo_personas, memory, memory_retrieval, memory_write, runtime_flags, voice, voice_chat as vc
    from app.config import settings
    oldyes = vc.is_yes

    def old_is_yes(t):
        t = f" {t.lower()} "
        return any(k in t for k in vc._YES) and not any(k in t for k in vc._NO)

    def english_template_with_devanagari():
        old = voice._TEMPLATES[("order_failed", "en")]
        voice._TEMPLATES[("order_failed", "en")] = old + " आपका भुगतान बाकी है।"
        return lambda: voice._TEMPLATES.__setitem__(("order_failed", "en"), old)

    def promise_days_changed():
        old = runtime_flags.get_promise_max_days_b2b
        runtime_flags.get_promise_max_days_b2b = lambda: 45
        return lambda: setattr(runtime_flags, "get_promise_max_days_b2b", old)

    def no_overlap_gate():
        a, b = settings.retrieval_min_overlap, settings.retrieval_confident_score
        settings.retrieval_min_overlap, settings.retrieval_confident_score = 0.0, 0.0
        return lambda: (setattr(settings, "retrieval_min_overlap", a), setattr(settings, "retrieval_confident_score", b))

    def drop_last_turn():
        old = memory_write.build_windows
        memory_write.build_windows = lambda turns, size=None: old(turns[:-1], size)
        return lambda: setattr(memory_write, "build_windows", old)

    def ignore_consent():
        old = contacts.has_full_memory_consent
        contacts.has_full_memory_consent = lambda cp: cp is not None
        return lambda: setattr(contacts, "has_full_memory_consent", old)

    def forget_does_nothing():
        old = memory.forget
        memory.forget = lambda db, contact_id, reason="requested": 0
        return lambda: setattr(memory, "forget", old)

    def expiry_ignored():
        old = memory_retrieval._episode_rows
        from sqlalchemy import text
        def unfiltered(db, ids, now=None):
            if not ids:
                return {}
            rows = db.execute(text("SELECT id, started_at, importance, summary FROM conversation_episodes WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}).all()
            return {str(r[0]): r for r in rows}
        memory_retrieval._episode_rows = unfiltered
        return lambda: setattr(memory_retrieval, "_episode_rows", old)

    def checker_allows_all():
        old = vc.validate_reply
        vc.validate_reply = lambda *a, **k: None
        return lambda: setattr(vc, "validate_reply", old)

    def summaries_never_written():
        old = memory_write.summarise_episode
        memory_write.summarise_episode = lambda *a, **k: {}
        return lambda: setattr(memory_write, "summarise_episode", old)

    from app import autopay, order_links, payment_links

    def link_payment_booked_twice():
        old = payment_links._record_payment_once
        def no_dedupe(db, link, payment_id, amount_inr):
            old(db, link, None, amount_inr)
            return True
        payment_links._record_payment_once = no_dedupe
        return lambda: setattr(payment_links, "_record_payment_once", old)

    def debit_after_settle():
        old = autopay._run_still_open
        autopay._run_still_open = lambda db, action: (True, "")
        return lambda: setattr(autopay, "_run_still_open", old)

    def link_outlives_offer():
        old = order_links.offer_snapshot
        def far(db, order):
            o = old(db, order)
            if o:
                o["ends_at"] = None
            return o
        order_links.offer_snapshot = far
        return lambda: setattr(order_links, "offer_snapshot", old)

    from contextlib import contextmanager
    from app import accounts, demo

    def finish_spends_real_links():
        old = demo._customer_ignores_us
        @contextmanager
        def nothing_held_back():
            yield
        demo._customer_ignores_us = nothing_held_back
        return lambda: setattr(demo, "_customer_ignores_us", old)

    def customer_reads_any_mandate():
        old = accounts._own_mandate
        from app.models import Mandate
        def anyone(db, customer, mandate_id):
            return db.query(Mandate).filter(Mandate.id == mandate_id).first()
        accounts._own_mandate = anyone
        return lambda: setattr(accounts, "_own_mandate", old)

    from app import promises as promises_mod

    def short_replies_ignored():
        return _patch(vc, "_short_turn", lambda *a, **k: None)

    def tiny_groups_become_patterns():
        from app import collections_insights as ci
        return _patch(ci, "MIN_GROUP", 0)

    def stand_in_links_counted():
        from app import collections_insights as ci
        from app.models import PaymentLink
        orig = ci.link_conversion

        def counting(db, since):
            db.query(PaymentLink).update({PaymentLink.is_stub: False})
            db.flush()
            return orig(db, since)
        return _patch(ci, "link_conversion", counting)

    def promise_does_not_extend_link():
        old = promises_mod._keep_link_alive
        promises_mod._keep_link_alive = lambda *a, **k: None
        return lambda: setattr(promises_mod, "_keep_link_alive", old)

    return [
        ("a checker that allows every reply", "guardrails", checker_allows_all),
        ("the yes-reader forgets Devanagari", "understanding", lambda: _patch(vc, "is_yes", old_is_yes)),
        ("an English line containing Hindi script", "fixed_lines", english_template_with_devanagari),
        ("a setting changes but the policy text does not", "policy_integrity", promise_days_changed),
        ("the answerable gate is removed", "retrieval", no_overlap_gate),
        ("the last turn is dropped from the stored conversation", "memory_write", drop_last_turn),
        ("memory ignores consent", "memory_read", ignore_consent),
        ("an expired call can still be recalled", "memory_read", expiry_ignored),
        ("forget does nothing", "privacy", forget_does_nothing),
        ("the reply checker is switched off", "flow", checker_allows_all),
        ("seeded call summaries are never written", "personas", summaries_never_written),
        ("a link payment is applied twice", "razorpay_money", link_payment_booked_twice),
        ("a debit fires on a settled run", "razorpay_money", debit_after_settle),
        ("a link outlives the offer", "razorpay_money", link_outlives_offer),
        ("a promise does not keep the link alive", "razorpay_money", promise_does_not_extend_link),
        ("run-to-the-end spends real links and debits", "accounts_console", finish_spends_real_links),
        ("a customer reads another customer's mandate", "accounts_console", customer_reads_any_mandate),
        ("short replies (haan / nahh / hmm) not read", "flow", short_replies_ignored),
        ("a pattern is drawn from 3 cases a side", "collections_analysis", tiny_groups_become_patterns),
        ("stand-in payment links count as conversions", "collections_analysis", stand_in_links_counted),
    ]


def main() -> int:
    harness.configure_eval_database()
    harness.ensure_database()
    harness.init_schema()
    ctx = harness.Context(log=False)
    rows, ok = [], True
    for name, suite, make in mutations():
        harness.wipe()
        undo = make()
        try:
            r = importlib.import_module(f"evals.suites.{suite}").run(ctx)
        except Exception as e:           # a crash also counts as "noticed", but say so
            caught, how = True, f"crashed ({type(e).__name__})"
        else:
            caught, how = (not r.passed), f"{len(r.failed_cases)} case(s) failed" if not r.passed else "suite stayed GREEN"
        finally:
            undo()
        harness.wipe()
        ok &= caught
        rows.append((name, suite, caught, how))
        print(f"{'caught ' if caught else 'MISSED '} [{suite}] {name} -> {how}")
    print("\nAll mutations were caught." if ok else "\nSome mutations were NOT caught: those suites cannot be trusted yet.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

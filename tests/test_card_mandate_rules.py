"""Card mandates we debit ourselves: a retry never lands inside Razorpay's
36h05m pre-debit window, and the default 25h floor is unchanged."""
from datetime import datetime, timedelta

from app.models import FailureReason
from app.rules_engine import CARD_TOKEN_DEBIT_LEAD, MANDATE_MIN_RETRY_GAP, rule_based_mandate_plan

NOW = datetime(2026, 10, 20, 4, 30)   # 10:00 IST


def _plan(reason, min_gap=MANDATE_MIN_RETRY_GAP):
    return rule_based_mandate_plan(failure_reason=reason, attempts_made=1, opened_at=NOW, now=NOW,
                                   max_retries=4, window_days=10, min_gap=min_gap)


def test_card_retry_respects_debit_lead():
    for reason in (FailureReason.INSUFFICIENT_FUNDS, FailureReason.BANK_DECLINE, FailureReason.UNKNOWN):
        p = _plan(reason, CARD_TOKEN_DEBIT_LEAD)
        assert p["terminal"] is None
        assert p["retry_at"] >= NOW + CARD_TOKEN_DEBIT_LEAD


def test_default_floor_unchanged():
    p = _plan(FailureReason.BANK_DECLINE)
    assert NOW + MANDATE_MIN_RETRY_GAP <= p["retry_at"] < NOW + CARD_TOKEN_DEBIT_LEAD + timedelta(days=1)
    assert p["notice_at"] == p["retry_at"] - timedelta(hours=24)


def test_min_gap_can_never_go_below_25h():
    p = _plan(FailureReason.BANK_DECLINE, timedelta(hours=1))
    assert p["retry_at"] >= NOW + MANDATE_MIN_RETRY_GAP

"""app/customer_profile.py: the rules that turn history into an approach (pure, no database)."""
from datetime import date, datetime, timedelta

from app import customer_profile as cpf, persona


def test_pay_day_and_slot_are_read_from_plain_words():
    assert cpf.parse_pay_day("Money usually arrives around day 5 of the month.") == 5
    assert cpf.parse_pay_day("paise 12 tareekh ko aate hain") == 12
    assert cpf.parse_pay_day("around day 45") is None
    assert cpf.slot_from_text("Prefers to be called in the evening.") == "evening"
    assert cpf.slot_from_text("whenever") is None


def test_approach_priority_and_consent():
    d = lambda **k: cpf.derive(k.pop("facts", {}), k.pop("kept", 0), k.pop("broken", 0), False, k.pop("disputed", False),
                               k.pop("tone", None), k.pop("consented", True))
    assert d(facts={"hardship_flag": "x"}, broken=2).approach == "gentle"       # difficulty beats a missed promise
    assert d(disputed=True).approach == "explain_first"
    assert d(broken=1).approach == "steady"
    assert d(kept=1).approach == "brief"
    assert d(kept=1, broken=1).approach == "standard"
    p = d(facts={"hardship_flag": "x", "salary_day": "day 5"}, consented=False)
    assert p.approach == "standard" and p.pay_day is None                        # facts need consent


def test_suggested_date_stays_inside_the_horizon():
    assert cpf.suggested_date(5, date(2026, 10, 10), 30) == date(2026, 11, 5)
    assert cpf.suggested_date(5, date(2026, 10, 10), 7) is None
    assert cpf.suggested_date(31, date(2026, 6, 10), 30) == date(2026, 6, 30)


def test_call_slot_never_leaves_the_bounds_or_the_calling_window():
    base = datetime(2026, 10, 24, 4, 0)
    moved = cpf.shift_to_slot(base, "evening", base - timedelta(hours=1), base + timedelta(days=1))
    assert 8 <= (moved + timedelta(hours=5, minutes=30)).hour < 19
    assert cpf.shift_to_slot(base, "evening", base + timedelta(hours=1), base + timedelta(hours=2)) == base


def test_fillers_are_rationed_and_never_back_to_back():
    assert not persona.wants_touch("standard", ["Namaste", "Acha, theek hai."], "yeh kitna hai batao")     # not twice in a row
    assert not persona.wants_touch("standard", ["Namaste"], "haan")                                         # short answer, short reply
    assert persona.wants_touch("gentle", ["Namaste", "Haan ji, bilkul."][:1] + ["x"], "yeh kitna hai batao")  # gentle softens more often


def test_dragon_hd_gets_only_pace_and_pauses():
    from app import speech
    t = "Main samajh sakti hoon... aisa ho jata hai. Aap bas ek date bata dijiye."
    assert speech.hd_delivery_ssml(t, "standard") == speech.escape(t)                       # the plain voice is left alone
    g = speech.hd_delivery_ssml(t, "gentle")
    assert "rate='-3%'" in g and "<break" in g and "pitch=" not in g and "express-as" not in g
    assert "rate='+6%'" in speech.hd_delivery_ssml(t, "brief")

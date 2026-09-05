"""
Lets you flip the LLM-failure simulation on/off live during a demo via
an API call, instead of editing .env and restarting the server.
Starts at whatever SIMULATE_LLM_FAILURE is set to in .env.

Also holds live-overridable values for the incentive-economics parameters
that /insights/apply-suggestion can change (see app/insights_router.py) -
same reasoning as the LLM-failure flag: flip them live during a demo
instead of editing .env and restarting. Each starts at whatever
config.py/.env has it set to. These are process-local and in-memory only -
they do NOT persist across a restart unless someone also updates .env by
hand.
"""
from app.config import settings

_llm_failure_forced = settings.simulate_llm_failure

_nudge_expiry_hours = settings.nudge_expiry_hours
_cart_idle_after_minutes = settings.cart_idle_after_minutes
_incentive_eligible_tiers = settings.incentive_eligible_tier_set()  # set[str]

# Per-tier discount bands, replacing the old single flat incentive_discount_pct.
# A customer's exact % inside their band comes from their engagement score
# (tiering.incentive_pct_for_customer) - still deterministic, still never
# chosen by the LLM.
_incentive_pct_bands = {
    "casual": [settings.incentive_pct_casual_min, settings.incentive_pct_casual_max],
    "regular": [settings.incentive_pct_regular_min, settings.incentive_pct_regular_max],
    "loyal": [settings.incentive_pct_loyal_min, settings.incentive_pct_loyal_max],
}

# Per-tier 30-day frequency caps, replacing the old flat
# incentive_max_per_customer_30d. Inverted against the bands above on
# purpose - see config.py.
_incentive_freq_caps = {
    "casual": settings.incentive_max_per_30d_casual,
    "regular": settings.incentive_max_per_30d_regular,
    "loyal": settings.incentive_max_per_30d_loyal,
    # Never consulted for these two - they can't reach offer_incentive at
    # all - but present so a lookup for any tier is total rather than a
    # KeyError waiting on an unrelated code path.
    "new": 0,
    "risk": 0,
}

# Per-tier order-value caps for incentives, replacing the old single
# incentive_max_order_value_inr. Scales up with tier - see config.py for
# why, and why every value stays under the high-value review threshold.
_incentive_amount_caps = {
    "casual": settings.incentive_max_order_value_casual,
    "regular": settings.incentive_max_order_value_regular,
    "loyal": settings.incentive_max_order_value_loyal,
    # Same never-consulted-but-total-lookup convention as the freq caps.
    "new": 0,
    "risk": 0,
}


def is_llm_failure_forced() -> bool:
    return _llm_failure_forced


def set_llm_failure_forced(value: bool) -> None:
    global _llm_failure_forced
    _llm_failure_forced = value


def get_incentive_pct_band(tier: str) -> tuple[int, int] | None:
    """None for tiers with no discount band at all (new/risk)."""
    band = _incentive_pct_bands.get(tier)
    return (band[0], band[1]) if band else None


def get_incentive_pct_bands() -> dict[str, list[int]]:
    return {k: list(v) for k, v in _incentive_pct_bands.items()}


def set_incentive_pct_band(tier: str, low: int, high: int) -> None:
    if tier not in _incentive_pct_bands:
        raise KeyError(f"{tier} has no incentive band")
    _incentive_pct_bands[tier] = [low, high]


def get_incentive_max_per_30d(tier: str) -> int:
    return _incentive_freq_caps.get(tier, 0)


def get_incentive_freq_caps() -> dict[str, int]:
    return dict(_incentive_freq_caps)


def set_incentive_max_per_30d(tier: str, value: int) -> None:
    if tier not in ("casual", "regular", "loyal"):
        raise KeyError(f"{tier} has no tunable frequency cap")
    _incentive_freq_caps[tier] = value


# --- Flat accessors for the per-tier bands/caps -----------------------------
# The dict-based getters above are the natural shape for the decision path
# (look up one tier, get its numbers). apply-suggestion needs the opposite
# shape: one flat "param name -> value" pair it can validate and set
# generically, like every other tunable. These shims bridge the two so the
# per-tier economics stay merchant-tunable without special-casing them in
# the router.
#
# Without these, the Incentive Analysis LLM would have no discount lever at
# all: its old one was the single flat incentive_discount_pct, which the
# per-tier bands replaced.

def _band_bound(tier: str, edge: str) -> int:
    return _incentive_pct_bands[tier][0 if edge == "min" else 1]


def _set_band_bound(tier: str, edge: str, value: int) -> None:
    _incentive_pct_bands[tier][0 if edge == "min" else 1] = value


def get_incentive_pct_casual_min() -> int: return _band_bound("casual", "min")
def get_incentive_pct_casual_max() -> int: return _band_bound("casual", "max")
def get_incentive_pct_regular_min() -> int: return _band_bound("regular", "min")
def get_incentive_pct_regular_max() -> int: return _band_bound("regular", "max")
def get_incentive_pct_loyal_min() -> int: return _band_bound("loyal", "min")
def get_incentive_pct_loyal_max() -> int: return _band_bound("loyal", "max")


def set_incentive_pct_casual_min(v: int) -> None: _set_band_bound("casual", "min", v)
def set_incentive_pct_casual_max(v: int) -> None: _set_band_bound("casual", "max", v)
def set_incentive_pct_regular_min(v: int) -> None: _set_band_bound("regular", "min", v)
def set_incentive_pct_regular_max(v: int) -> None: _set_band_bound("regular", "max", v)
def set_incentive_pct_loyal_min(v: int) -> None: _set_band_bound("loyal", "min", v)
def set_incentive_pct_loyal_max(v: int) -> None: _set_band_bound("loyal", "max", v)


def get_incentive_max_per_30d_casual() -> int: return _incentive_freq_caps["casual"]
def get_incentive_max_per_30d_regular() -> int: return _incentive_freq_caps["regular"]
def get_incentive_max_per_30d_loyal() -> int: return _incentive_freq_caps["loyal"]


def set_incentive_max_per_30d_casual(v: int) -> None: set_incentive_max_per_30d("casual", v)
def set_incentive_max_per_30d_regular(v: int) -> None: set_incentive_max_per_30d("regular", v)
def set_incentive_max_per_30d_loyal(v: int) -> None: set_incentive_max_per_30d("loyal", v)


def get_incentive_max_order_value(tier: str) -> int:
    return _incentive_amount_caps.get(tier, 0)


def get_incentive_amount_caps() -> dict[str, int]:
    return {t: _incentive_amount_caps[t] for t in ("casual", "regular", "loyal")}


def set_incentive_max_order_value(tier: str, value: int) -> None:
    if tier not in ("casual", "regular", "loyal"):
        raise ValueError(f"No incentive amount cap for tier '{tier}'")
    _incentive_amount_caps[tier] = value


def get_incentive_max_order_value_casual() -> int: return _incentive_amount_caps["casual"]
def get_incentive_max_order_value_regular() -> int: return _incentive_amount_caps["regular"]
def get_incentive_max_order_value_loyal() -> int: return _incentive_amount_caps["loyal"]


def set_incentive_max_order_value_casual(v: int) -> None: set_incentive_max_order_value("casual", v)
def set_incentive_max_order_value_regular(v: int) -> None: set_incentive_max_order_value("regular", v)
def set_incentive_max_order_value_loyal(v: int) -> None: set_incentive_max_order_value("loyal", v)


def get_nudge_expiry_hours() -> int:
    return _nudge_expiry_hours


def set_nudge_expiry_hours(value: int) -> None:
    global _nudge_expiry_hours
    _nudge_expiry_hours = value


def get_cart_idle_after_minutes() -> int:
    return _cart_idle_after_minutes


def set_cart_idle_after_minutes(value: int) -> None:
    global _cart_idle_after_minutes
    _cart_idle_after_minutes = value


def get_incentive_eligible_tiers() -> set[str]:
    return set(_incentive_eligible_tiers)


def set_casual_tier_incentive_eligible(enabled: bool) -> None:
    """The only tier toggle exposed to apply-suggestion. 'casual' is the
    only tier that can ever be added/removed this way - regular and loyal
    stay permanently in the set, and new/risk can never enter it (see
    config.py's incentive_eligible_tier_set(), which this intersects
    against as a second, defense-in-depth guardrail)."""
    global _incentive_eligible_tiers
    allowed_universe = {"casual", "regular", "loyal"}
    tiers = set(_incentive_eligible_tiers)
    if enabled:
        tiers.add("casual")
    else:
        tiers.discard("casual")
    _incentive_eligible_tiers = tiers & allowed_universe


# --- Payment-failure / dropoff policy params (Payment-Failure Analysis) ---

_confidence_threshold = settings.confidence_threshold
_max_auto_retries = settings.max_auto_retries
_high_value_amount_inr = settings.high_value_amount_inr


def get_confidence_threshold() -> float:
    return _confidence_threshold


def set_confidence_threshold(value: float) -> None:
    global _confidence_threshold
    _confidence_threshold = value


def get_max_auto_retries() -> int:
    return _max_auto_retries


def set_max_auto_retries(value: int) -> None:
    global _max_auto_retries
    _max_auto_retries = value


def get_high_value_amount_inr() -> int:
    return _high_value_amount_inr


def set_high_value_amount_inr(value: int) -> None:
    global _high_value_amount_inr
    _high_value_amount_inr = value


# --- Tiering: engagement-score model (Tier Analysis) ---
# Replaces the old success-rate thresholds entirely. See app/tiering.py
# for why payment success rate is no longer what a tier is made of.

_tier_loyal_score = settings.tier_loyal_score
_tier_regular_score = settings.tier_regular_score
_tier_min_attempts_for_loyal = settings.tier_min_attempts_for_loyal
_tier_min_attempts_for_regular = settings.tier_min_attempts_for_regular
_tier_target_orders_per_month = settings.tier_target_orders_per_month
_tier_target_aov_inr = settings.tier_target_aov_inr
_tier_recency_window_days = settings.tier_recency_window_days
_tier_behavior_window_days = settings.tier_behavior_window_days
_tier_risk_min_attempts = settings.tier_risk_min_attempts
_tier_risk_attributable_failure_rate = settings.tier_risk_attributable_failure_rate
_tier_risk_cancel_rate = settings.tier_risk_cancel_rate


def get_tier_loyal_score() -> int:
    return _tier_loyal_score


def set_tier_loyal_score(value: int) -> None:
    global _tier_loyal_score
    _tier_loyal_score = value


def get_tier_regular_score() -> int:
    return _tier_regular_score


def set_tier_regular_score(value: int) -> None:
    global _tier_regular_score
    _tier_regular_score = value


def get_tier_min_attempts_for_loyal() -> int:
    return _tier_min_attempts_for_loyal


def set_tier_min_attempts_for_loyal(value: int) -> None:
    global _tier_min_attempts_for_loyal
    _tier_min_attempts_for_loyal = value


def get_tier_min_attempts_for_regular() -> int:
    return _tier_min_attempts_for_regular


def set_tier_min_attempts_for_regular(value: int) -> None:
    global _tier_min_attempts_for_regular
    _tier_min_attempts_for_regular = value


def get_tier_target_orders_per_month() -> float:
    return _tier_target_orders_per_month


def set_tier_target_orders_per_month(value: float) -> None:
    global _tier_target_orders_per_month
    _tier_target_orders_per_month = value


def get_tier_target_aov_inr() -> int:
    return _tier_target_aov_inr


def set_tier_target_aov_inr(value: int) -> None:
    global _tier_target_aov_inr
    _tier_target_aov_inr = value


def get_tier_recency_window_days() -> int:
    return _tier_recency_window_days


def set_tier_recency_window_days(value: int) -> None:
    global _tier_recency_window_days
    _tier_recency_window_days = value


def get_tier_behavior_window_days() -> int:
    return _tier_behavior_window_days


def set_tier_behavior_window_days(value: int) -> None:
    global _tier_behavior_window_days
    _tier_behavior_window_days = value


def get_tier_risk_min_attempts() -> int:
    return _tier_risk_min_attempts


def set_tier_risk_min_attempts(value: int) -> None:
    global _tier_risk_min_attempts
    _tier_risk_min_attempts = value


def get_tier_risk_attributable_failure_rate() -> float:
    return _tier_risk_attributable_failure_rate


def set_tier_risk_attributable_failure_rate(value: float) -> None:
    global _tier_risk_attributable_failure_rate
    _tier_risk_attributable_failure_rate = value


def get_tier_risk_cancel_rate() -> float:
    return _tier_risk_cancel_rate


def set_tier_risk_cancel_rate(value: float) -> None:
    global _tier_risk_cancel_rate
    _tier_risk_cancel_rate = value

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Groq (free tier, no credit card required - console.groq.com)
    groq_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/revenue_recovery"

    # --- Payment failure thresholds ---
    confidence_threshold: float = 0.70
    high_value_amount_inr: int = 10000
    max_auto_retries: int = 3

    # --- Drop-off / checkout abandonment thresholds ---
    dropoff_poll_interval_minutes: int = 10       # how often the poller runs
    dropoff_abandonment_window_minutes: int = 10  # order older than this = abandoned
    dropoff_lookback_days: int = 7                # window for counting repeat abandonments

    # --- Incentive gate (all three must pass for offer_incentive to auto-execute) ---
    # Amount cap is per tier now, like the discount bands and frequency
    # caps: higher tiers have earned incentives on bigger carts. Scales UP
    # with tier (unlike the inverted frequency caps) but every cap stays
    # below the high-value human-review threshold, so an incentive never
    # auto-applies in the range where a failed payment would already be
    # routed to a human. The cap keeps holding while an offer rides an
    # edited cart: the discount suspends whenever the live cart total
    # exceeds the customer's tier cap, and reactivates under it.
    incentive_max_order_value_casual: int = 2000
    incentive_max_order_value_regular: int = 3000
    incentive_max_order_value_loyal: int = 4000

    # Discount % is no longer one flat number. Each tier gets a BAND, and a
    # customer's exact % inside their own band is derived from their
    # engagement score (see tiering.incentive_pct_for_customer). Still
    # fully deterministic - the LLM never picks a discount, same rule as
    # before, just a formula instead of a constant.
    incentive_pct_casual_min: int = 0
    incentive_pct_casual_max: int = 10
    incentive_pct_regular_min: int = 10
    incentive_pct_regular_max: int = 20
    incentive_pct_loyal_min: int = 20
    incentive_pct_loyal_max: int = 30

    # 30-day frequency cap, per tier. Deliberately INVERTED against the
    # discount bands above: LOYAL gets the biggest discount but the fewest
    # shots at it, CASUAL the smallest discount but the most. If frequency
    # AND discount AND cancel-tolerance all scaled up together, the tier
    # with the most room to exploit them would get all three at once.
    incentive_max_per_30d_casual: int = 3
    incentive_max_per_30d_regular: int = 2
    incentive_max_per_30d_loyal: int = 1

    # --- Cart-event nudges (timeout / explicit cancel) ---
    # How long a cancel-resume card stays valid, and how long a timeout nudge
    # can still be credited with causing a later order. Backend-enforced only
    # - never rendered as a visible countdown.
    nudge_expiry_hours: int = 24

    # --- Cart-idle sweep (app/storefront.py::run_cart_idle_sweep) ---
    # The real, automatic version of POST /debug/simulate-cart-timeout: a cart
    # that's had items sitting in it, untouched, this long gets treated as a
    # silent abandonment - same pipeline the debug button fires manually.
    cart_idle_after_minutes: int = 30
    cart_idle_sweep_interval_minutes: int = 5

    # --- Customer tiering (app/tiering.py) ---
    # These decide who counts as loyal/at-risk, which the whole recovery
    # ladder (incentive eligibility, escalation reasoning, etc.) is built on
    # top of. Changing these doesn't just tune one action's behavior the way
    # every other param here does - it changes what a customer's tier IS.
    #
    # Tiers are no longer driven by raw payment success rate. A failed
    # payment is very often the gateway's or the issuer's problem, not the
    # customer's, and tiering someone down for it punishes people for
    # infrastructure they don't control - worse, it punished the customers
    # who RETRIED hardest (see purchase_attempts() in tiering.py). Tier is
    # now an engagement score built from behaviour the customer actually
    # controls: do they finish the carts they start, how often do they buy,
    # how much do they spend, how recently, and do they respond to nudges.
    tier_loyal_score: int = 70        # engagement score (0-100) needed for LOYAL
    tier_regular_score: int = 40      # engagement score needed for REGULAR

    # Volume floors, so one lucky order can't score its way to the top.
    # A single successful first order otherwise scores near-perfectly on
    # every ratio-based component at once.
    tier_min_attempts_for_loyal: int = 5
    tier_min_attempts_for_regular: int = 3

    # Engagement score component calibration.
    tier_target_orders_per_month: float = 1.0   # what "a regular buyer" looks like
    tier_target_aov_inr: int = 2000             # order value that maxes the monetary component
    tier_recency_window_days: int = 90          # no purchase in this long -> recency component hits 0
    tier_behavior_window_days: int = 180        # cancels/abandons older than this stop counting at all

    # RISK gate - evaluated BEFORE the score ladder, never reachable by
    # scoring badly. Two independent entry paths (plus the permanent
    # risk_block flag), each needing a minimum sample first.
    tier_risk_min_attempts: int = 2
    tier_risk_attributable_failure_rate: float = 0.60   # share of attempts failing for customer-side reasons
    tier_risk_cancel_rate: float = 0.60                 # share of purchase intents ending in an explicit cancel

    # --- Maintenance sweep (app/maintenance.py) ---
    tier_refresh_interval_minutes: int = 30
    stale_order_abandon_after_hours: int = 24

    # --- Which customer tiers can ever see offer_incentive from the
    # cart-event ladder (rule_based_cart_event_decision). Comma-separated,
    # e.g. "loyal,regular". This is a genuine policy dial - see
    # incentive_eligible_tier_set() below, which is the ONLY place this
    # string is read. NEW and RISK are hardcoded out there regardless of
    # what this value contains: that's a deliberate anti-abuse / low-signal
    # guardrail (see rules_engine.py), not a tunable economics parameter,
    # so it can't be reopened by editing .env or via an LLM suggestion.
    #
    # Default now includes casual: with per-tier discount bands, CASUAL's
    # band tops out at 10% rather than sharing one flat rate with LOYAL,
    # so opting them in no longer means paying a top-tier discount to a
    # bottom-tier customer - which was the whole reason the old default
    # kept the lowest tier out. ---
    incentive_eligible_tiers: str = "loyal,regular,casual"

    def incentive_eligible_tier_set(self) -> set[str]:
        allowed_universe = {"casual", "regular", "loyal"}  # new/risk hardcoded out, always
        configured = {t.strip() for t in self.incentive_eligible_tiers.split(",") if t.strip()}
        return configured & allowed_universe

    # --- Demo failure injection ---
    simulate_llm_failure: bool = False

    # --- Storefront (customer-facing demo) ---
    storefront_secret_key: str = "dev-only-change-me"   # signs session tokens - fine hardcoded for a demo


settings = Settings()

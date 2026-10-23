from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # Payment Links (app/payment_links.py): "auto" = real links when the keys
    # exist, "real" = always real (missing keys is an error, never a quiet
    # stub), "stub" = clearly-labelled stand-ins, nothing reaches Razorpay.
    # Seeds, evals and bulk CSV import force stub themselves. Test Mode allows
    # only razorpay_test_link_cap links per business, so the Demo Console counts
    # them and at the cap we fall back to a labelled stand-in (audited) rather
    # than send a message with no way to pay.
    razorpay_links: str = "auto"
    razorpay_test_link_cap: int = 30
    # Demo Console only: how far ahead of "now" a REAL Razorpay step may sit before the
    # console warns. A working rule of thumb, not a documented Razorpay limit (their docs
    # state none for links or tokens) - keep real steps close together when testing.
    test_mode_window_days: int = 3

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

    # --- Mandate retry sequencer (app/mandate.py) ---
    # Cap on merchant-driven retries of one failed recurring debit, and how
    # long after the first failure a retry may still be scheduled.
    mandate_max_retries: int = 4
    mandate_retry_window_days: int = 10

    # --- B2B receivables chaser (app/receivables.py) ---
    # Days relative to due date for the four automated chase steps
    # (courtesy reminder, payment-link email, email+SMS, voice call), then the
    # day past due on which the invoice goes to a human (finance).
    receivables_chase_offsets_days: str = "-3,1,7,15"
    receivables_escalate_after_days: int = 30

    # --- Environment + access (app/merchant_auth.py, otp.py, voice.py, dnd.py) ---
    # "production" turns the demo conveniences into hard refusals: the
    # simulated OTP provider (which returns the code in the response), the
    # simulated voice provider and the simulated DND registry are all refused,
    # so a fake verification or a fake "call placed" can't ship by accident.
    environment: str = "development"

    # Opt-in protection for every merchant-side endpoint. Empty = open (the
    # demo default); set it and those endpoints need the X-Merchant-Key header.
    merchant_api_key: str = ""
    # The one merchant login, created on first start (app/accounts.py::bootstrap_merchant); never changes an existing password.
    merchant_email: str = ""
    merchant_password: str = ""
    merchant_name: str = "Backstop Store"

    # Do-Not-Disturb registry (NCPR). v1 is simulated: a comma-separated list
    # of numbers treated as registered, for testing the block. A real
    # provider / registry lookup replaces SimulatedDndChecker in app/dnd.py.
    dnd_provider: str = "simulated"
    dnd_simulated_numbers: str = ""

    # --- OTP (app/otp.py) ---
    # "simulated" returns the code in the API response (demo/test only) and
    # sends nothing. A real SMS vendor is one class implementing OtpProvider
    # and needs DLT template registration for India.
    otp_provider: str = "simulated"   # simulated | msg91

    # Demo Console speech (browser-side convenience, never a phone line).
    # auto = use the cloud voice/listener when its key is present, else the browser's own.
    voice_tts_provider: str = "auto"   # auto | browser | azure
    voice_stt_provider: str = "auto"   # auto | browser | groq
    # Retrieval (merchant knowledge + customer memory). Embeddings are computed locally (no API key).
    rag_enabled: bool = True
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    # A passage is worth showing the model only when it clears the cosine floor AND either shares enough words with the question
    # or is a strong semantic match (measured by evals/suites/retrieval.py: this rejects ~9 in 10 off-topic questions while keeping
    # ~8 in 10 real ones; the cosine floor alone rejected 4 in 10).
    retrieval_min_overlap: float = 0.15
    retrieval_confident_score: float = 0.40
    retrieval_min_score: float = 0.22      # cosine floor for showing a passage to the model; the model + validator are the precision filter
    memory_retention_days: int = 180
    # Tiered memory (memory_write.py / memory_retrieval.py): episodes, verbatim chunks and facts all expire after
    # memory_retention_days. The read budget caps how much remembered text one turn may carry to the model.
    memory_chunk_turns: int = 4              # turns per word-for-word window
    memory_budget_chars: int = 2000          # hard cap on remembered text handed to the model per turn
    memory_half_life_days: int = 60          # recency weight halves every this many days
    memory_top_k: int = 3                    # verbatim windows / episodes returned on a recall question
    default_merchant_id: str = "default"   # single-merchant today; every RAG row carries it so tenancy is a filter, not a rewrite
    persona_gender: str = "female"     # matches the Azure voices (Neerja / Swara); male flips fixed lines
    tts_cache_dir: str = "var/tts_cache"
    azure_speech_key: str = ""
    azure_speech_region: str = ""      # e.g. centralindia
    # Dragon HD voices are the most natural (their pitch and rhythm follow the MEANING of the text, so they take plain punctuated
    # text; of the SSML tuning only pace and pauses are obeyed, never pitch or styles). Not in Azure's documented free allowance:
    # if one is refused or over quota the call falls back to azure_voice_fallback (a standard voice).
    azure_voice_hi: str = "en-IN-Meera:DragonHDLatestNeural"          # multilingual: also reads Devanagari
    azure_voice_en: str = "en-IN-Meera:DragonHDLatestNeural"
    azure_voice_hinglish: str = "en-IN-Meera:DragonHDLatestNeural"
    azure_voice_fallback: str = "en-IN-NeerjaNeural"
    azure_hd_delivery: bool = True     # per-customer pace and pauses on a Dragon HD voice (undocumented by Microsoft, measured to work; see speech.is_hd)
    groq_stt_model: str = "whisper-large-v3"
    stt_language_mode: str = "fixed"   # fixed = tell Whisper the call's language; auto = let it detect (see scripts/eval_stt.py)
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 30
    otp_max_sends_per_phone_per_hour: int = 5

    # --- Outbound text messages (app/messaging.py) ---
    # simulated: every message is stored in outbound_messages and shown in the
    #   Demo Console inbox; nothing leaves the machine.
    # msg91:     sent through MSG91's Flow API (needs a DLT-approved template
    #   id per message kind; a kind with no template id is recorded as failed,
    #   never silently dropped or sent as free text).
    messaging_provider: str = "simulated"
    msg91_auth_key: str = ""
    msg91_sender_id: str = ""
    msg91_otp_template_id: str = ""
    # one DLT template id per message kind; variables are sent as var1..varN
    # in the order given by messaging.TEMPLATE_VARS
    msg91_tpl_pre_debit_notice: str = ""
    msg91_tpl_payment_link: str = ""
    msg91_tpl_remandate_link: str = ""
    msg91_tpl_promise_reminder: str = ""
    msg91_tpl_invoice_courtesy: str = ""
    msg91_tpl_invoice_overdue: str = ""
    msg91_tpl_payment_link_offer: str = ""
    msg91_tpl_order_offer_ended: str = ""
    # Used when a link must be absolute (SMS bodies). Local default is fine
    # for the sandbox; set to your public URL for anything else.
    public_base_url: str = "http://localhost:8000"

    # --- Collections insights (app/collections_insights.py) ---
    # Below this many resolved cases a metric is "not enough data", never a
    # recommendation.
    collections_min_sample: int = 20

    # --- Voice recovery (app/voice.py) ---
    # Consumer calls are only ever about a FAILED PAYMENT (never an abandoned
    # cart), above this order value, still unresolved after the wait below.
    voice_min_order_value: int = 1000
    voice_failed_order_wait_hours: int = 4
    # Total automated calls per debt before it goes to a human. (One per day
    # and the 08:00-19:00 IST window are hardcoded guardrails, not tunable.)
    voice_max_attempts_per_subject: int = 3

    # --- Promise-to-pay (app/promises.py) ---
    # Longest a customer may promise ahead (consumers vs businesses), how
    # many broken promises on one debt before we stop accepting new ones, and
    # how long after the promised date we wait before calling it broken.
    promise_max_days_b2c: int = 7
    promise_max_days_b2b: int = 30
    promise_max_broken: int = 2
    promise_grace_days: int = 1

    # --- Scheduler (app/scheduler.py) ---
    scheduler_interval_seconds: int = 60

    # --- Demo failure injection ---
    simulate_llm_failure: bool = False

    # --- Storefront (customer-facing demo) ---
    storefront_secret_key: str = "dev-only-change-me"   # signs session tokens - fine hardcoded for a demo


def production_problems(s: "Settings") -> list[str]:
    """Why this configuration is not safe to run as environment=production.
    Empty list = fine (and always empty outside production)."""
    if s.environment != "production":
        return []
    problems = []
    if s.storefront_secret_key == "dev-only-change-me":
        problems.append("STOREFRONT_SECRET_KEY is still the default - it signs OTP proofs, "
                        "customer sessions and self-serve links, so anyone could forge them")
    if not s.merchant_api_key:
        problems.append("MERCHANT_API_KEY is empty - every merchant endpoint would be open")
    if not s.razorpay_webhook_secret:
        problems.append("RAZORPAY_WEBHOOK_SECRET is empty - webhooks cannot be verified")
    if s.razorpay_links == "stub":
        problems.append("RAZORPAY_LINKS is 'stub' - customers would get stand-in payment links")
    if s.razorpay_key_id.startswith("rzp_test_"):
        problems.append("RAZORPAY_KEY_ID is a Test Mode key - no real money would move")
    if s.messaging_provider == "simulated":
        problems.append("MESSAGING_PROVIDER is 'simulated' - notices and payment links "
                        "would be recorded but never delivered")
    return problems


settings = Settings()

import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Float, Integer, DateTime, Date, Enum, ForeignKey, Boolean, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


class FailureReason(str, enum.Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    BANK_DECLINE = "bank_decline"
    RISK_BLOCK = "risk_block"
    NETWORK_ERROR = "network_error"
    AUTHENTICATION_FAILED = "authentication_failed"
    CANCELLED = "cancelled"
    INVALID_CARD = "invalid_card"
    UNKNOWN = "unknown"


class AgentAction(str, enum.Enum):
    # --- Payment failure actions ---
    RETRY_NOW = "retry_now"
    RETRY_LATER = "retry_later"
    SEND_NUDGE = "send_nudge"
    # --- Drop-off / checkout abandonment actions ---
    SEND_REMINDER = "send_reminder"
    SEND_RESUME_LINK = "send_resume_link"
    OFFER_INCENTIVE = "offer_incentive"
    NO_ACTION = "no_action"
    # --- Shared across both pipelines ---
    ESCALATE_TO_HUMAN = "escalate_to_human"
    RULE_DEFAULT_FALLBACK = "rule_default_fallback"  # used only when LLM path fails
    # --- Scheduler-driven channel actions (mandate / receivables / voice) ---
    # Chosen ONLY by deterministic rules + the scheduler (app/scheduler.py),
    # never offered to the LLM - see SCHEDULER_ONLY_ACTIONS below.
    SEND_PRE_DEBIT_NOTICE = "send_pre_debit_notice"
    SEND_PAYMENT_LINK = "send_payment_link"
    SEND_REMANDATE_LINK = "send_remandate_link"
    VOICE_CALL = "voice_call"


# Excluded from the LLM tool schema (llm_agent.VALID_ACTIONS) so the model can
# never pick them: the LLM stays bounded to the original recovery actions.
SCHEDULER_ONLY_ACTIONS = frozenset({
    AgentAction.SEND_PRE_DEBIT_NOTICE,
    AgentAction.SEND_PAYMENT_LINK,
    AgentAction.SEND_REMANDATE_LINK,
    AgentAction.VOICE_CALL,
})


class ScheduledKind(str, enum.Enum):
    MANDATE_RETRY = "mandate_retry"
    PRE_DEBIT_NOTICE = "pre_debit_notice"
    PROMISE_REMINDER = "promise_reminder"
    PROMISE_CHECK = "promise_check"
    INVOICE_CHASE = "invoice_chase"
    VOICE_CALL = "voice_call"
    # Failed storefront order: the ONE reminder on the day the customer named
    # (not a promise - a shopper owes nothing). Contact kind.
    ORDER_LINK_REMINDER = "order_link_reminder"
    # Card mandates we debit ourselves (app/autopay.py). Not contact kinds: the
    # bank pre-debit notice is Razorpay's, our own SMS is PRE_DEBIT_NOTICE.
    MANDATE_PDN = "mandate_pdn"          # create the debit order (bank notice starts now)
    MANDATE_DEBIT = "mandate_debit"      # charge it, once the notice window has passed


class ScheduledStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    DONE = "done"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class EventType(str, enum.Enum):
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_PENDING = "subscription.pending"
    SUBSCRIPTION_HALTED = "subscription.halted"
    SUBSCRIPTION_CHARGED = "subscription.charged"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    # Customer-initiated, not Razorpay-reported - the one deliberate
    # exception to "Event only ever represents something Razorpay knows
    # about". Written once per /checkout/give-up-failed
    # call as its OWN new Event+Decision row, specifically so it never
    # overwrites the outcome of the failed-payment Events it resolves -
    # each retry's own decision (retry_now, escalated, ...) stays visible
    # and untouched; this is a new, separate line in the feed.
    PAYMENT_FAILURE_GIVEN_UP = "payment_failure_given_up"


class Event(Base):
    """Raw, normalized record of any revenue-loss event (webhook or poller-detected)."""
    __tablename__ = "events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    razorpay_event_id = Column(String, nullable=True, unique=True, index=True)
    event_type = Column(Enum(EventType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    subscription_id = Column(String, nullable=True, index=True)
    payment_id = Column(String, nullable=True, index=True)
    customer_id = Column(String, nullable=True, index=True)
    amount_inr = Column(Float, nullable=False, default=0.0)

    # The Razorpay order this event belongs to, when known. Distinct from
    # razorpay_event_id (the webhook delivery's own id) and payment_id
    # (Razorpay's payment id, which the storefront's own Order doesn't
    # store at failure time). This is what lets analysis code join a
    # payment-failure Event straight to the storefront Order it's about
    # (Order.razorpay_order_id) - without it, that join has no clean key
    # and needs fragile heuristics (see the attempt-count fix earlier this
    # project). Null for synthetic/non-storefront events, e.g.
    # scripts/simulate_webhook.py payloads with no real backing Order.
    razorpay_order_id = Column(String, nullable=True, index=True)

    # --- Payment failure fields (null for drop-off events) ---
    failure_reason = Column(Enum(FailureReason, values_callable=lambda x: [e.value for e in x]), nullable=True)
    attempt_count = Column(Integer, nullable=True)
    payment_method = Column(String, nullable=True)   # card / netbanking / wallet / upi, as reported by Razorpay

    # --- Raw Razorpay error fields, preserved verbatim (never invented/normalized here) ---
    razorpay_error_code = Column(String, nullable=True)
    razorpay_error_description = Column(Text, nullable=True)
    razorpay_error_reason = Column(String, nullable=True)
    razorpay_error_source = Column(String, nullable=True)
    razorpay_error_step = Column(String, nullable=True)

    # --- Drop-off / checkout abandonment fields (null for failure events) ---
    checkout_status = Column(String, nullable=True)       # "created" | "attempted"
    minutes_since_created = Column(Integer, nullable=True)
    abandonment_count = Column(Integer, nullable=True)    # Nth abandonment in lookback window

    raw_payload = Column(Text, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow)

    decisions = relationship("Decision", back_populates="event")


class Decision(Base):
    """What the agent decided to do about an event, and why."""
    __tablename__ = "decisions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    event_id = Column(UUID(as_uuid=False), ForeignKey("events.id"), nullable=False)
    action = Column(Enum(AgentAction, values_callable=lambda x: [e.value for e in x]), nullable=False)
    confidence = Column(Float, nullable=False)
    reasoning = Column(Text, nullable=False)
    source = Column(String, nullable=False)  # "rules_engine" | "llm_agent"
    escalated = Column(Boolean, default=False)
    executed = Column(Boolean, default=False)
    outcome = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    event = relationship("Event", back_populates="decisions")


class CustomerTier(str, enum.Enum):
    """Five states in ONE register - all five describe what kind of account
    this is, so NEW and RISK don't read as odd ones out next to an
    award-metal ladder (the earlier bronze/silver/gold naming).

    CASUAL -> REGULAR -> LOYAL is the climbable ladder, driven by a single
    engagement score (see app/tiering.py). NEW ("no history yet") and RISK
    ("recovery effort not warranted") sit deliberately OFF that ladder:
    they're states, not rungs, and neither is reachable by scoring
    slightly better or worse.
    """
    NEW = "new"
    CASUAL = "casual"
    REGULAR = "regular"
    LOYAL = "loyal"
    RISK = "risk"


class OrderStatus(str, enum.Enum):
    CREATED = "created"      # razorpay order created, checkout not yet completed
    CAPTURED = "captured"    # payment succeeded
    FAILED = "failed"        # payment attempted and failed
    # Checkout was started but never resolved either way, and is now old
    # enough that it never will be. Set only by the maintenance sweep
    # (app/maintenance.py), never by a payment path. Exists so stale
    # CREATED rows become an explicit, queryable state instead of sitting
    # forever in a status that means "still in progress" - every
    # aggregation already filters to CAPTURED/FAILED, so this changes no
    # existing number, it just stops the ambiguity accumulating.
    ABANDONED = "abandoned"


class CartEventType(str, enum.Enum):
    SILENT_ABANDON = "silent_abandon"      # cart sat idle, no explicit action (simulated via timeout button)
    EXPLICIT_CANCEL = "explicit_cancel"    # customer actively deleted/cancelled the cart


class CartEventStatus(str, enum.Enum):
    """Only meaningful for events that produced something the customer can
    act on (an explicit-cancel resume/incentive card). Silent-abandon events
    and no-action/reminder-only cancels leave this null - there's nothing to
    resolve."""
    PENDING = "pending"
    RESUMED = "resumed"
    DECLINED = "declined"
    EXPIRED = "expired"
    SUPERSEDED_BY_NEW_ORDER = "superseded_by_new_order"


class PendingSignalKind(str, enum.Enum):
    # Invisible bookkeeping: lets /checkout attribute an order to a silent-
    # abandon nudge shown earlier, with no click for the customer to make.
    TIMEOUT_ATTRIBUTION = "timeout_attribution"
    # Visible: a one-shot toast the cart page shows once, about a payment
    # that failed while the customer wasn't watching the checkout screen.
    PAYMENT_FAILURE_NOTICE = "payment_failure_notice"


class Customer(Base):
    """A storefront customer/shopper. Separate from Razorpay's customer_id concept —
    this is OUR record used for auth, cart, order history, and tiering."""
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    tier = Column(Enum(CustomerTier, values_callable=lambda x: [e.value for e in x]),
                  default=CustomerTier.NEW, nullable=False)
    # Cart is deliberately just a JSON blob on the customer row - this project's
    # focus is transaction/recovery logic, not a real product catalog/cart engine.
    cart_json = Column(Text, nullable=False, default="[]")
    # Last time cart_json was written (add/remove/clear/resume) - drives the
    # cart-idle sweep (app/storefront.py::run_cart_idle_sweep). NOT NULL with a
    # server_default (the "hard" convention, like Order.risk_settled below) on
    # purpose: a NULL here would silently and permanently exempt a row from the
    # sweep's `<` cutoff comparison.
    cart_updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                              server_default=text("(now() at time zone 'utc')"))
    created_at = Column(DateTime, default=datetime.utcnow)

    # Contact + consent, used only by the outreach channels (voice, SMS).
    # All nullable / defaulted so existing rows stay valid; the ALTERs live in
    # sql/2026_10_recovery_extensions.sql (create_all won't add columns).
    phone = Column(String, nullable=True)
    preferred_language = Column(String, nullable=False, default="hinglish", server_default="hinglish")  # en | hi | hinglish
    voice_consent = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    consent_at = Column(DateTime, nullable=True)

    orders = relationship("Order", back_populates="customer")
    cart_events = relationship("CartEvent", back_populates="customer")


class Order(Base):
    """A storefront order created via the real Razorpay Orders API.
    Distinct from the generic `Event` table used by the webhook/dropoff pipelines -
    this table exists so the storefront can show a customer's own order history
    and so tiering has something concrete to aggregate over."""
    __tablename__ = "storefront_orders"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False, index=True)
    razorpay_order_id = Column(String, nullable=True, index=True)
    razorpay_payment_id = Column(String, nullable=True)
    items_json = Column(Text, nullable=False, default="[]")
    amount_inr = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
                     default=OrderStatus.CREATED, nullable=False)
    failure_reason = Column(Enum(FailureReason, values_callable=lambda x: [e.value for e in x]), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)   # set when captured or failed

    # Meaningful for FAILED orders only: "this order's run-level at-risk
    # chapter is closed" - released to recovered (a later capture of the
    # same basket), released to lost (explicit cancel consolidation or the
    # maintenance sweep's lapse), or never booked at all because it was a
    # retry of a run whose first failure already carries the booking. The
    # exactly-once guard for the failed-payment side of the ledger, exactly
    # parallel to revenue_recorded below for the capture side - deliberately
    # a SEPARATE flag: revenue_recorded gates capture-revenue claiming, and
    # overloading it would break the same-order modal-retry case.
    risk_settled = Column(Boolean, nullable=False, default=False, server_default="false")

    # Set once by revenue.record_capture_revenue when this order's capture has
    # been booked into the merchant ledger. This is the exactly-once guard
    # that keeps /checkout/verify and the payment.captured webhook from
    # either double-booking or - the old bug - each assuming the other had
    # done it. order.status alone can't play this role: it says the state
    # transition happened, not that the money accounting did.
    revenue_recorded = Column(Boolean, nullable=False, default=False, server_default="false")

    # Set at order-creation time (not at capture) when this order exists
    # because of a prior cart-event nudge/offer - either the customer clicked
    # "resume" on an explicit-cancel card, or a still-valid timeout-attribution
    # marker was found for this customer. Null for ordinary, un-nudged orders.
    recovered_from_cart_event_id = Column(UUID(as_uuid=False), ForeignKey("cart_events.id"), nullable=True)

    customer = relationship("Customer", back_populates="orders")
    recovered_from_cart_event = relationship("CartEvent", foreign_keys=[recovered_from_cart_event_id])


class CartEvent(Base):
    """Pre-checkout funnel events: a cart that never became a Razorpay order at all.
    Kept separate from `Event`/dropoff pipeline, which only ever sees orders that
    were at least created in Razorpay. See CartEventType for the two flavors -
    they get materially different recovery treatment."""
    __tablename__ = "cart_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False, index=True)
    event_type = Column(Enum(CartEventType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    items_json = Column(Text, nullable=False, default="[]")
    amount_inr = Column(Float, nullable=False, default=0.0)
    tier_at_time = Column(Enum(CustomerTier, values_callable=lambda x: [e.value for e in x]), nullable=False)
    action = Column(Enum(AgentAction, values_callable=lambda x: [e.value for e in x]), nullable=True)
    confidence = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    outcome = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # --- Only populated for explicit_cancel events that produced a
    # resume/incentive card (offer_incentive or send_resume_link). Null for
    # everything else - silent_abandon events and no_action/reminder-only
    # cancels never have anything for the customer to resolve. ---
    status = Column(Enum(CartEventStatus, values_callable=lambda x: [e.value for e in x]), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    # Snapshotted at decision time so the terms shown to the customer (and
    # the amount actually charged) never drift if config changes later.
    incentive_pct = Column(Float, nullable=True)
    incentive_final_amount_inr = Column(Float, nullable=True)

    # The three independent incentive gates, snapshotted at decision time -
    # previously collapsed into one ambiguous "incentive_eligible" boolean
    # that only ever reflected the frequency cap and wasn't even stored on
    # this row (audit-log-only). Stored as real columns, not just audit
    # JSON, specifically so merchant-facing analysis (app/insights.py) can
    # query "how often was each gate the actual blocker" directly instead
    # of parsing text. Null for events where the gate concept doesn't apply
    # (e.g. tiers with no incentive branch at all, like risk/new).
    amount_cap_ok = Column(Boolean, nullable=True)
    freq_cap_ok = Column(Boolean, nullable=True)
    tier_incentive_eligible = Column(Boolean, nullable=True)

    customer = relationship("Customer", back_populates="cart_events")


class MerchantRevenueState(Base):
    """Single-row rolling ledger of the merchant's revenue position.
    Every field here is only ever mutated alongside a matching audit_log entry -
    see app/revenue.py."""
    __tablename__ = "merchant_revenue_state"

    id = Column(Integer, primary_key=True, default=1)
    total_revenue = Column(Float, nullable=False, default=0.0)        # confirmed captured, all-time
    at_risk_soft = Column(Float, nullable=False, default=0.0)         # silent abandons / nudges: real recovery targets
    at_risk_declined = Column(Float, nullable=False, default=0.0)     # explicit cancels: tracked, weighted down in recovery %
    at_risk_failed = Column(Float, nullable=False, default=0.0, server_default="0")  # failed payments: own thread, own exits
    at_risk_mandate = Column(Float, nullable=False, default=0.0, server_default="0")  # failed recurring debits: own thread, exits via MandateRun.risk_settled
    # B2B receivables: a SEPARATE ledger section, deliberately NOT part of
    # total_at_risk or the consumer recovery rate (one big invoice would
    # drown a thousand carts, and a business paying 45 days late is normal
    # where a cart abandoned for 45 days is gone). Same rules otherwise: every
    # mutation goes through revenue.adjust() and is audited.
    receivables_overdue = Column(Float, nullable=False, default=0.0, server_default="0")
    receivables_collected = Column(Float, nullable=False, default=0.0, server_default="0")
    receivables_written_off = Column(Float, nullable=False, default=0.0, server_default="0")
    total_recovered = Column(Float, nullable=False, default=0.0)      # at-risk amount that later converted
    total_lost = Column(Float, nullable=False, default=0.0)           # given up as unrecoverable
    incentive_cost = Column(Float, nullable=False, default=0.0)       # money given away via discounts, tracked explicitly
    updated_at = Column(DateTime, default=datetime.utcnow)


class WebhookEventLog(Base):
    """Idempotency ledger for inbound Razorpay webhooks - covers EVERY event
    type (payment.captured, payment.failed, anything else), not just the ones
    that create an Event row. Razorpay uses at-least-once delivery, so every
    webhook must be checked against this table before any processing happens.

    razorpay_event_id comes from the X-Razorpay-Event-Id header on real
    traffic. Local dev tools that don't set that header (scripts/simulate_webhook.py
    predating this change, manual curl tests) fall back to a hash of the raw
    body, which still gives correct dedup behavior for identical redeliveries
    while treating genuinely different payloads as distinct."""
    __tablename__ = "webhook_event_log"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    razorpay_event_id = Column(String, nullable=False, unique=True, index=True)
    event_type = Column(String, nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow)


class PendingSignal(Base):
    """Lightweight, mostly-invisible marker used to bridge a decision made
    *now* to a customer action that might happen much later (or never).

    Two uses, distinguished by `kind`:
    - timeout_attribution: written when a silent-abandon nudge is shown.
      Never surfaced to the UI directly - checked only by /checkout, so an
      eventual order (whenever it happens, however it happens) can be
      honestly linked back to the nudge that (maybe) caused it.
    - payment_failure_notice: written once a payment-failure decision
      resolves. Surfaced once as a toast when the customer returns to the
      cart page, then consumed.

    Both kinds expire - see app.config.settings.nudge_expiry_hours - so
    attribution/messaging is never claimed across an implausibly long gap.
    """
    __tablename__ = "pending_signals"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False, index=True)
    kind = Column(Enum(PendingSignalKind, values_callable=lambda x: [e.value for e in x]), nullable=False)

    cart_event_id = Column(UUID(as_uuid=False), ForeignKey("cart_events.id"), nullable=True)
    order_id = Column(UUID(as_uuid=False), ForeignKey("storefront_orders.id"), nullable=True)

    action = Column(String, nullable=True)
    reasoning = Column(Text, nullable=True)
    failure_reason = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)


class MandateStatus(str, enum.Enum):
    ACTIVE = "active"          # failed cycle, retries/communications in flight
    RECOVERED = "recovered"    # a later debit succeeded (subscription.charged)
    EXHAUSTED = "exhausted"    # retry cap / window hit - handed to a human
    HALTED = "halted"          # subscription halted; waiting on a re-mandate
    CANCELLED = "cancelled"    # closed with no recovery (lapsed re-mandate, merchant cancel)


class MandateRun(Base):
    """One failed billing cycle of a recurring debit (UPI Autopay / e-mandate /
    tokenised card), from first failure to recovered-or-lost.

    Own money thread: the full cycle amount is booked into
    MerchantRevenueState.at_risk_mandate exactly once when the run opens and
    leaves exactly once through risk_settled (atomic claim, same pattern as
    Order.risk_settled) - either total_recovered or total_lost. Never crosses
    the cart / declined / failed-payment buckets."""
    __tablename__ = "mandate_runs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    subscription_id = Column(String, nullable=False, index=True)
    razorpay_customer_id = Column(String, nullable=True)
    amount_inr = Column(Float, nullable=False, default=0.0)
    failure_reason = Column(String, nullable=True)
    attempts_made = Column(Integer, nullable=False, default=1)
    status = Column(String, nullable=False, default=MandateStatus.ACTIVE.value, index=True)
    risk_booked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    risk_settled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    opened_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    next_action_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    # Set for a card mandate we debit ourselves (Mandate row); then
    # subscription_id holds its token id so every existing query keeps working.
    # NULL = a Razorpay Subscription run (simulator / Demo Console scenario).
    mandate_id = Column(UUID(as_uuid=False), nullable=True, index=True)


class MandateState(str, enum.Enum):
    SETUP = "setup"            # registration link sent, customer has not authorised yet
    ACTIVE = "active"          # token confirmed - we may debit it
    REJECTED = "rejected"      # authorisation failed / token rejected at the mandate hub
    CANCELLED = "cancelled"    # customer / bank / merchant cancelled the token
    PAUSED = "paused"


class Mandate(Base):
    """A card mandate on Razorpay's recurring API that WE debit (app/autopay.py).

    Razorpay holds the token, sends the bank pre-debit notice and moves the
    money; our scheduler decides WHEN each debit happens (MANDATE_PDN then
    MANDATE_DEBIT), so a promise or a retry rule moves a real debit. A failed
    cycle opens an ordinary MandateRun (mandate_id set) - same money thread,
    same notices, promises and sweep as a Subscription run."""
    __tablename__ = "mandates"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    customer_name = Column(String, nullable=False)
    contact = Column(String, nullable=True)          # what Razorpay gets; ContactPoint stays the
    email = Column(String, nullable=True)            # source of truth for whether WE may contact
    razorpay_customer_id = Column(String, nullable=True, index=True)
    registration_invoice_id = Column(String, nullable=True, index=True)   # inv_... (auth link)
    registration_url = Column(String, nullable=True)
    token_id = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, default=MandateState.SETUP.value, index=True)
    amount_inr = Column(Float, nullable=False)       # one cycle
    max_amount_inr = Column(Float, nullable=False)
    frequency = Column(String, nullable=False, default="monthly")
    next_due_at = Column(DateTime, nullable=True)
    # Who started it from the customer page (app/account.py); NULL for ones the
    # Demo Console / merchant created. service_* is what they chose to pay for.
    customer_id = Column(UUID(as_uuid=False), nullable=True, index=True)
    service_key = Column(String, nullable=True)
    service_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AppUser(Base):
    """A login that is not a storefront shopper: the merchant (one) and the
    invoice customers (a business) the merchant registers. Kept out of
    `customers` on purpose - that table feeds tiering, the engagement sweep and
    the dashboard's shopper lists, none of which should ever see these rows."""
    __tablename__ = "app_users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False, index=True)      # merchant | business
    business_account_id = Column(UUID(as_uuid=False), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PaymentLinkStatus(str, enum.Enum):
    CREATED = "created"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"    # replaced by a newer link for the same debt (amount/offer changed)


class PaymentLink(Base):
    """Every Razorpay Payment Link this app hands a customer (app/payment_links.py),
    one OPEN row per debt at most. Created lazily - only when a message is about
    to carry it - and reused, because Test Mode caps a business at ~30 links.
    `is_stub` rows are labelled stand-ins (stub mode, or the cap was hit): the
    inbox says so and nothing can be paid through them.

    original_amount_inr / offer_* snapshot an offer the cart ALREADY had (failed
    storefront orders only); a link never outlives that offer (expire_at)."""
    __tablename__ = "payment_links"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    razorpay_link_id = Column(String, nullable=True, unique=True, index=True)
    short_url = Column(String, nullable=True)
    subject_type = Column(String, nullable=False, index=True)    # invoice | order | mandate_run
    subject_id = Column(String, nullable=False, index=True)
    amount_inr = Column(Float, nullable=False)
    original_amount_inr = Column(Float, nullable=True)
    offer_pct = Column(Float, nullable=True)
    offer_ends_at = Column(DateTime, nullable=True)
    expire_at = Column(DateTime, nullable=False)
    status = Column(String, nullable=False, default=PaymentLinkStatus.CREATED.value, index=True)
    is_stub = Column(Boolean, nullable=False, default=False)
    stub_reason = Column(String, nullable=True)                  # stub_mode | limit_reached | error
    amount_paid_inr = Column(Float, nullable=False, default=0.0)
    paid_payment_ids = Column(Text, nullable=False, default="[]")
    refunded_payment_ids = Column(Text, nullable=False, default="[]")   # duplicate-basket refunds
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class InvoiceStatus(str, enum.Enum):
    OPEN = "open"                      # issued, not yet due
    OVERDUE = "overdue"                # past due, money booked to receivables_overdue
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    DISPUTED = "disputed"              # chase stopped; a human must decide
    WRITTEN_OFF = "written_off"


class BusinessAccount(Base):
    """A B2B client. Deliberately NOT a Customer row: they never log into the
    storefront and must not pollute consumer tiering."""
    __tablename__ = "business_accounts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    company_name = Column(String, nullable=False)
    gstin = Column(String, nullable=True)
    contact_name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    preferred_language = Column(String, nullable=False, default="hinglish")   # en | hi | hinglish
    voice_consent = Column(Boolean, nullable=False, default=False)
    payment_terms_days = Column(Integer, nullable=False, default=30)
    credit_limit_inr = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    invoices = relationship("Invoice", back_populates="account")


class Invoice(Base):
    """One receivable. Money thread (all via revenue.adjust):
      crosses due_at   -> receivables_overdue += outstanding   (once, overdue_booked)
      payment          -> overdue -> receivables_collected     (partial allowed)
      write-off/credit -> overdue -> receivables_written_off
    `settled` is the atomic exactly-once guard on the final exit. Invoices are
    never auto-lost: B2B debts are written off by a person, not a timer."""
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    account_id = Column(UUID(as_uuid=False), ForeignKey("business_accounts.id"), nullable=False, index=True)
    invoice_no = Column(String, nullable=False, unique=True, index=True)
    amount_inr = Column(Float, nullable=False)
    amount_paid = Column(Float, nullable=False, default=0.0)
    issued_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    due_at = Column(DateTime, nullable=False, index=True)
    status = Column(String, nullable=False, default=InvoiceStatus.OPEN.value, index=True)
    razorpay_payment_link_id = Column(String, nullable=True, index=True)
    payment_link_url = Column(String, nullable=True)
    overdue_booked = Column(Boolean, nullable=False, default=False)
    overdue_booked_amount = Column(Float, nullable=False, default=0.0)
    overdue_released = Column(Float, nullable=False, default=0.0)   # booked amount already moved out
    settled = Column(Boolean, nullable=False, default=False)
    escalated_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    paid_payment_ids = Column(Text, nullable=False, default="[]")   # json list, payment-level dedupe
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("BusinessAccount", back_populates="invoices")


class PromiseStatus(str, enum.Enum):
    OPEN = "open"
    KEPT = "kept"
    PARTIALLY_KEPT = "partially_kept"
    BROKEN = "broken"
    CANCELLED = "cancelled"


class PaymentPromise(Base):
    """A customer's commitment to pay a specific at-risk amount by a date.

    A promise is a STATUS OVERLAY on money that is already at risk - it never
    moves a rupee itself. The debt stays in whatever bucket it was in until a
    real payment (-> recovered) or a final give-up (-> lost) moves it. That is
    what keeps the "every at-risk rupee leaves through exactly one exit" rule
    intact: a customer can promise, break it and promise again without the
    same money being counted twice or vanishing."""
    __tablename__ = "payment_promises"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    subject_type = Column(String, nullable=False, index=True)   # mandate_run | invoice
    subject_id = Column(String, nullable=False, index=True)
    customer_ref = Column(String, nullable=True)
    amount_inr = Column(Float, nullable=False, default=0.0)
    promised_date = Column(Date, nullable=False)
    captured_via = Column(String, nullable=False, default="merchant_manual")  # voice | self_serve_link | merchant_manual
    status = Column(String, nullable=False, default=PromiseStatus.OPEN.value, index=True)
    amount_paid = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


class ContactPoint(Base):
    """The single source of truth for "can we call this person, and how".

    Keyed by whichever identity the debt carries - our storefront customer,
    Razorpay's customer id (subscription/mandate debits), or a B2B account -
    so every subject (failed order, mandate run, invoice) resolves its contact
    the same way (app/contacts.py).

    Voice consent is deliberately only ever CUSTOMER-originated:
    consent_source can only be "customer_ticked", and consent can only be
    recorded for a phone that passed an OTP check (phone_verified_at). A
    merchant can supply the number and language, never the consent."""
    __tablename__ = "contact_points"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=True, unique=True, index=True)
    razorpay_customer_id = Column(String, nullable=True, unique=True, index=True)
    business_account_id = Column(UUID(as_uuid=False), ForeignKey("business_accounts.id"),
                                 nullable=True, unique=True, index=True)
    phone = Column(String, nullable=True)                       # E.164, +91XXXXXXXXXX
    phone_verified_at = Column(DateTime, nullable=True)
    preferred_language = Column(String, nullable=False, default="hinglish")   # en | hi | hinglish
    voice_consent = Column(Boolean, nullable=False, default=False)
    consent_at = Column(DateTime, nullable=True)
    consent_source = Column(String, nullable=True)              # customer_ticked
    consent_text_version = Column(String, nullable=True)
    # Separate from call consent: may the assistant keep short notes (preferred time, when money
    # arrives...) to make FUTURE calls easier? Customer-ticked, deletable, expires.
    memory_consent = Column(Boolean, nullable=False, default=False)
    memory_consent_at = Column(DateTime, nullable=True)
    memory_consent_version = Column(String, nullable=True)      # wording the customer agreed to (see contacts.MEMORY_CONSENT_TEXT_VERSION)
    wrong_number_at = Column(DateTime, nullable=True)           # heard "wrong number" on a call
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OtpChallenge(Base):
    """One OTP sent to one phone for one purpose. The code is stored hashed
    (HMAC), never in clear; a challenge dies on success, expiry, or too many
    wrong guesses (app/otp.py)."""
    __tablename__ = "otp_challenges"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    phone = Column(String, nullable=False, index=True)
    purpose = Column(String, nullable=False)       # register | profile | self_serve
    code_hash = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class VoiceTurn(Base):
    """One line of a multi-turn interactive call. `agent` lines are always text
    WE wrote (script / fixed answers); `customer` lines are what was said."""
    __tablename__ = "voice_turns"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    call_id = Column(String, nullable=False, index=True)
    turn_no = Column(Integer, nullable=False)
    speaker = Column(String, nullable=False)          # agent | customer
    text = Column(Text, nullable=False)
    intent = Column(String, nullable=True)            # customer lines: what we understood
    kind = Column(String, nullable=True)              # agent lines: answer | clarify | handoff | closing
    timings_json = Column(Text, nullable=True)        # {"stt_ms","llm_ms","retrieve_ms","server_ms","tts_ms"}
    interrupted = Column(Boolean, nullable=False, default=False)   # customer talked over this agent line
    sources = Column(Text, nullable=True)             # JSON list of approved passages a policy answer used
    created_at = Column(DateTime, default=datetime.utcnow)


class VoiceCall(Base):
    """One automated recovery call (simulated provider in v1). The script is a
    deterministic template; the LLM only ever classifies the transcript into a
    fixed intent enum (app/voice.py)."""
    __tablename__ = "voice_calls"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    subject_type = Column(String, nullable=False, index=True)   # invoice | mandate_run
    subject_id = Column(String, nullable=False, index=True)
    customer_ref = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    language = Column(String, nullable=False, default="hinglish")
    script_text = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="completed")  # completed | no_answer | failed
    transcript = Column(Text, nullable=True)
    intent = Column(String, nullable=True)
    intent_source = Column(String, nullable=True)                 # llm | keyword_fallback
    extracted_promise_date = Column(Date, nullable=True)
    routed_to = Column(String, nullable=True)
    duration_s = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class OutboundMessage(Base):
    """Every customer-facing text the system sends (or, in sandbox mode, WOULD
    send): who, which template, the exact words, and what happened to it.
    app/messaging.py is the only writer. In the simulated provider this table
    IS the "phone" - the Demo Console inbox reads it; with a real provider the
    same row records the provider's answer."""
    __tablename__ = "outbound_messages"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    channel = Column(String, nullable=False, default="sms")        # sms (voice lives in voice_calls)
    template = Column(String, nullable=False, index=True)          # pre_debit_notice | payment_link | ...
    subject_type = Column(String, nullable=True, index=True)       # mandate_run | invoice | order | otp
    subject_id = Column(String, nullable=True, index=True)
    to_phone = Column(String, nullable=True, index=True)           # E.164; null = nobody to send to
    language = Column(String, nullable=False, default="hinglish")
    body = Column(Text, nullable=False)
    link = Column(String, nullable=True)
    link_kind = Column(String, nullable=True)                      # self_serve | razorpay_payment_link | placeholder
    provider = Column(String, nullable=False)                      # simulated | msg91
    status = Column(String, nullable=False)                        # recorded | sent | failed | not_sent_no_contact
    provider_ref = Column(String, nullable=True)
    error = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ScheduledAction(Base):
    """A durable 'do X at time T' job. The project had no scheduler: RETRY_LATER
    was only a label. Every time-based feature (mandate retries, promise
    reminders/checks, invoice chase ladder, voice calls) writes rows here and
    app/scheduler.py fires them.

    Exactly-once: a row is claimed with an atomic
    UPDATE ... WHERE status='scheduled', same pattern as Order.revenue_recorded.
    kind/status are plain strings (values from ScheduledKind/ScheduledStatus)
    so adding a kind never needs an ALTER TYPE."""
    __tablename__ = "scheduled_actions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    kind = Column(String, nullable=False, index=True)
    subject_type = Column(String, nullable=False)       # mandate_run | invoice | promise | order
    subject_id = Column(String, nullable=False, index=True)
    customer_ref = Column(String, nullable=True)
    due_at = Column(DateTime, nullable=False, index=True)
    status = Column(String, nullable=False, default=ScheduledStatus.SCHEDULED.value, index=True)
    attempt_no = Column(Integer, nullable=False, default=1)
    payload_json = Column(Text, nullable=False, default="{}")
    result = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    executed_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    """Append-only, hash-chained log. Never update or delete rows from this table."""
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    sequence_num = Column(Integer, nullable=False, unique=True)
    event_id = Column(UUID(as_uuid=False), nullable=True)
    decision_id = Column(UUID(as_uuid=False), nullable=True)
    action_type = Column(String, nullable=False)
    details = Column(Text, nullable=False)
    prev_hash = Column(String, nullable=False)
    entry_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Float, Integer, DateTime, Enum, ForeignKey, Boolean, Text, text
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


class EventType(str, enum.Enum):
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_PENDING = "subscription.pending"
    SUBSCRIPTION_HALTED = "subscription.halted"
    SUBSCRIPTION_CHARGED = "subscription.charged"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    # Customer-initiated, not Razorpay-reported - the one deliberate
    # exception to "Event only ever represents something Razorpay knows
    # about" (see CLAUDE.md). Written once per /checkout/give-up-failed
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

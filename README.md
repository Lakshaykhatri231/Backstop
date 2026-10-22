# Backstop — AI Revenue Recovery Agent

An event-driven system that finds revenue slipping away — a failed payment, an abandoned checkout, a card autopay that bounced, an overdue
invoice — and works to win it back inside strict, auditable limits. Built on FastAPI, PostgreSQL and Razorpay (Test Mode), with a React
merchant dashboard, a customer storefront and an autopay page.

**The design in one paragraph.** A deterministic rules engine decides first. A language model may only *refine* that decision within fixed
bounds (it can adjust confidence and wording, never choose an action outside a closed set or a money amount). Risky or low-confidence cases
go to a human. Every state change — and every rupee that moves — is written once, through one function, to an append-only hash-chained
audit log. Customers are contacted only within consent, calling-hours and frequency guardrails that are enforced in code, not in prompts.

| | |
|---|---|
| **Backend** | Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL (≈23k lines, 100+ audited action types, ~150 HTTP endpoints) |
| **Frontend** | React 19 · TanStack Router/Query · Vite · Tailwind 4 · shadcn/radix (≈20k lines TypeScript) |
| **AI** | Groq (function-calling) for bounded decisions and policy analysis · local embeddings + pgvector/numpy hybrid retrieval · Azure neural TTS · Whisper STT |
| **Payments** | Razorpay Test Mode: Orders, Standard Checkout, Payment Links, card mandates (recurring API), HMAC-verified webhooks |
| **Quality** | 38 unit tests · 13 offline eval suites (~890 cases) · 1 live-model suite · 20 mutation self-tests proving the suites fail when the app is broken |

> **Status.** Everything runs end to end against Razorpay **Test Mode**. SMS, OTP, the DND registry and the telephone line are *simulated*
> (the browser is the phone). See [What is real and what is simulated](#what-is-real-and-what-is-simulated) — this document is explicit
> about the boundary, and `ENVIRONMENT=production` refuses to start with the simulated providers.

---

## Contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Design principles](#design-principles)
4. [Consumer recovery: the decision pipeline](#consumer-recovery-the-decision-pipeline)
5. [Rules, tiering, incentives, money, audit](#the-rules-engine-ladder-by-ladder)
6. [Autopay, promises, receivables](#autopay-promises-and-receivables)
7. [The Hinglish voice agent](#the-hinglish-voice-agent)
8. [Policy analytics: four self-tuning analyses](#policy-analytics-four-self-tuning-analyses)
9. [Razorpay integration](#razorpay-integration)
10. [Accounts and access](#accounts-and-access)
11. [Simulation Console](#simulation-console)
12. [Testing and evaluation](#testing-and-evaluation)
13. [Getting started](#getting-started)
14. [Configuration](#configuration)
15. [Operations](#operations)
16. [Security, privacy and compliance posture](#security-privacy-and-compliance-posture)
17. [What is real and what is simulated](#what-is-real-and-what-is-simulated)
18. [Limitations and roadmap](#limitations-and-roadmap)
19. [Repository layout](#repository-layout)

---

## What it does

| Capability | What happens | Where |
|---|---|---|
| **Payment-failure recovery** | A Razorpay `payment.failed` webhook (HMAC-verified) becomes an `Event`; rules pick retry-now / retry-later / nudge / escalate from the failure reason and attempt count; a bounded LLM may adjust confidence; a confidence gate can force a human. | `webhook.py`, `rules_engine.py`, `llm_agent.py` |
| **Checkout drop-off recovery** | No webhook exists for "order created, never paid", so a poller finds orders stuck in `attempted` and runs the same pipeline. | `dropoff.py` |
| **Pre-checkout cart recovery** | A cart abandoned before any Razorpay order exists. Silent abandon vs explicit cancel are different signal strengths with different recovery ladders; discounts come from a deterministic per-tier formula. | `storefront.py`, `tiering.py` |
| **Customer tiering** | An engagement score (not payment success rate) places customers on NEW / RISK / CASUAL / REGULAR / LOYAL and decides how much recovery effort and discount they get. | `tiering.py` |
| **Card autopay recovery** | Real card mandates on Razorpay's recurring API. *We* decide when to debit (a notice order ≥ 37 h ahead, then the debit); a failed debit opens a recovery run with dated retries, bank-style notices, a call before the retry, and a re-mandate link when the mandate is dead. | `autopay.py`, `mandate.py` |
| **Promise-to-pay tracker** | A customer names a date; chasing pauses, a reminder fires the day before, a check after the date marks it kept or broken (a broken one resumes one step firmer). A status overlay — it never moves money itself. | `promises.py` |
| **B2B receivables chaser** | Overdue invoices backed by Razorpay Payment Links (part-payments allowed), a firming chase ladder, disputes, aging, credit notes, write-offs by a person, and their own ledger buckets. | `receivables.py` |
| **Hinglish voice agent** | Multi-turn recovery calls in English / Hindi / Hinglish: it answers questions from merchant-approved documents, remembers a consenting customer between calls, understands short replies ("haan", "nahh", "ek minute"), asks for a date when none is given, and hands to a person when it should. | `voice.py`, `voice_chat.py`, `knowledge.py`, `memory*.py` |
| **Customer self-serve page** | A signed link where a customer verifies their number by OTP, picks a payment date and language, and gives (or withholds) call consent. | `selfserve.py` |
| **Policy analytics** | Four analyses (incentives, loss & recovery, tiers, collections) read real outcomes, propose settings changes with the evidence behind each, preview their impact, and apply them with an audit trail. | `*_insights*.py` |
| **Logins and portals** | One sign-in page for shoppers, autopay customers, business (invoice) customers and the merchant; a customer autopay page; a business invoices page; a merchant invoice register. | `accounts.py`, `auth.py` |
| **Simulation Console** | Step any invoice, autopay or failed order through its whole life — clock, queue, SMS inbox, a call you answer as the customer — using the real scheduler and guards. | `demo.py`, `clock.py` |

---

## Architecture

```mermaid
flowchart LR
    subgraph Signals
      W[Razorpay webhooks<br/>HMAC-verified]
      P[Drop-off poller]
      C[Storefront cart events]
      S[Scheduler<br/>durable jobs]
    end
    W & P & C --> R[Rules engine<br/>deterministic decision]
    R --> L[LLM refine<br/>bounded: confidence + wording only]
    L --> G{Confidence gate}
    G -- below bar / high value / risk --> H[Escalate to human]
    G -- ok --> X[Execute action<br/>stub layer, never claims recovered]
    S --> M[Mandate / promises /<br/>receivables / voice]
    M --> X
    X --> A[(Audit log<br/>append-only, hash-chained)]
    R --> A
    L --> A
    X --> V[(Revenue ledger<br/>single adjust function, exactly-once)]
    V --> A
    A --> I[Policy analytics<br/>4 analyses + apply]
    I -. changes settings via runtime flags .-> R
```

**Request lifecycle (payment failure).** `POST /webhooks/razorpay` verifies the HMAC signature, dedupes on the `X-Razorpay-Event-Id`
header, writes an `Event` and returns `accepted` immediately. A FastAPI background task then runs the decision with its own DB session:
rules → bounded LLM → confidence gate → action → audit entries at each step. Real recovery is only ever confirmed by a later
`payment.captured` webhook or an HMAC-verified checkout callback — never by the action layer.

**Four background threads start at boot** — each exists because some money or message would otherwise have no exit:

| Thread | Interval | Purpose |
|---|---|---|
| `dropoff-poller` | 10 min | Orders stuck in `attempted` past the abandonment window |
| `cart-idle-sweep` | 5 min | Carts untouched 30+ min → the silent-abandon pipeline |
| `maintenance-sweep` | 30 min | Stale orders → abandoned; tier refresh (time-decaying score parts); closes dead recovery paths so no rupee stays "at risk" forever; memory expiry; embedding refresh |
| `scheduler` | configurable | Fires durable `ScheduledAction` rows (retries, notices, reminders, chases, calls, debits) with an atomic claim — exactly once |

**Where the LLM can and cannot act**

| The model **may** | The model **cannot** |
|---|---|
| Adjust a decision's confidence and rewrite its human-readable reasoning | Choose an action outside the `AgentAction` enum (blocked by tool schema) |
| Classify a call transcript into a fixed intent enum | Pick or alter any money amount — discounts are a formula |
| Write a spoken reply that code then validates (numbers, wording, language, no links, no human claim) | Skip the validator: a rejected reply falls back to fixed text and is audited |
| Propose policy changes in the analytics layer | Apply them itself, or touch a setting outside the whitelist |

Any model failure — timeout, malformed tool call, rate limit, or the `/debug/toggle-llm-failure` switch — falls back to the rules engine (or a
fixed template) with an `llm_failure_fallback` audit entry. Nothing crashes and nothing goes silent.

---

## Design principles

1. **Deterministic first, AI second.** `rules_engine.py` is independently readable and testable; no model call ever happens inside it.
2. **Bounded AI.** Closed action enums, schema-constrained tool calls, code-side reply validation, whitelisted tunable parameters.
3. **Money moves once.** One `adjust()` function mutates the ledger; every rupee entering an at-risk bucket leaves through exactly one exit
   (recovered or lost); capture accounting is guarded by atomic claims (`revenue_recorded`, `risk_settled`) so two confirmation paths can't
   double-book.
4. **Everything is auditable.** Every state change goes through `write_audit_entry()` into an append-only, hash-chained table.
5. **Consent and timing are code, not prompts.** A call needs an OTP-verified number and consent the *customer* ticked (the merchant can
   never tick it), inside 08:00–19:00 IST, one call a day, an attempt cap, and a DND check. A changed number clears verification and consent.
6. **Honest about what is simulated.** Labelled stand-ins instead of fake links; "recorded" vs "sent" vs "failed" message states; production
   mode refuses simulated providers.
7. **No vendor SDKs.** Groq and Razorpay are called over plain `httpx` through one client module each, which also gives tests a single seam.
8. **Tunables are data, not code.** Thresholds are read live through `runtime_flags`, so an applied recommendation takes effect without a restart.

---

## Consumer recovery: the decision pipeline

Revenue leaks at three different points, and each needs a *different detection mechanism*:

| Signal | Why it needs its own detector | Entry point | Rules function | Rows written |
|---|---|---|---|---|
| **Payment failure** / subscription | Razorpay pushes it to us | `webhook.py` (HMAC-verified) | `rule_based_decision` | `Event` + `Decision` |
| **Checkout drop-off** | *Nothing fails*, so nothing fires — abandonment is the **absence** of a paid status, only findable by polling | `dropoff.py` (poller) | `rule_based_dropoff_decision` | `Event` + `Decision` |
| **Pre-checkout cart event** | No Razorpay order exists yet — nothing to receive *or* poll for | `storefront.py::_handle_cart_event` | `rule_based_cart_event_decision` | `CartEvent` only |

The third path is deliberately separate: an `Event` row only ever represents something Razorpay knows about. `CartEvent` splits into
`silent_abandon` vs `explicit_cancel` — different *signal strength*, not just different labels.

Every signal follows the same sequence: **record event → deterministic rules decision → LLM enrich (bounded) → confidence gate →
`execute_action` → audit at every step.**

1. **Rules decide first** (no LLM inside the rules engine).
2. **The LLM may only refine** — Groq function-calling, 8 s timeout, action constrained to the `AgentAction` enum by the tool schema.
3. **Any LLM failure falls back, loudly** — `source: "rules_engine_fallback"` plus an `llm_failure_fallback` audit entry.
4. **The confidence gate can override** — below `confidence_threshold` (**0.70**) a decision is forced to `escalate_to_human`.
5. **Execution is a stub layer, on purpose** — `actions.py` makes no real retry/email/SMS call and must never report "recovered".

**One deliberate asymmetry:** the payment-failure pipeline exempts both `escalate_to_human` *and* `retry_now` from the gate (gating
`retry_now` would kill the unknown/invalid-card path); the drop-off pipeline exempts only `escalate_to_human`. The cart-event path has no
LLM layer and no gate — its action is already bounded by the deterministic tier formula.

---

## The rules engine, ladder by ladder

All thresholds are read live through `runtime_flags`, so a change applied via
`/insights/apply-suggestion` takes effect without a restart. Confidence values are calibrated
against the **0.70** auto-execute bar.

### Payment failure — `rule_based_decision`

Evaluated top to bottom; first match wins.

| # | Condition | Action | Confidence |
|---|---|---|---|
| 1 | `attempt_count > max_auto_retries` (3) | `escalate_to_human` | 0.95 |
| 2 | `amount >= high_value_amount_inr` (₹5,000) | `escalate_to_human` | 0.60 |
| 3 | `risk_block` | `escalate_to_human` | 0.85 |
| 4 | `card_expired` | `send_nudge` | 0.90 |
| 5 | `cancelled` | `send_nudge` | 0.70 |
| 6 | `unknown` / `invalid_card`, 1st attempt | `retry_now` | 0.55 / 0.60 |
| 7 | `unknown` / `invalid_card`, 2nd+ attempt | `escalate_to_human` | 0.75 |
| 8 | `insufficient_funds`, 1st attempt | `retry_later` | 0.80 |
| 9 | `insufficient_funds`, 2nd+ attempt | `retry_later` | 0.55 |
| 10 | `bank_decline`/`network_error`/`authentication_failed`, 1st attempt | `retry_now` | 0.75 network, else 0.65 |
| 11 | same three, 2nd+ attempt | `retry_later` | 0.50 |
| 12 | unrecognised reason code | `escalate_to_human` | 0.30 |

Why: the retry cap is checked **first**, above even `risk_block`. The high-value floor sits
second, above every reason-specific branch, with its 0.60 confidence deliberately *below* the
0.70 bar. `risk_block` never gets an attempt-1 retry — unsafe at any count. `card_expired` and
`cancelled` are message-only (retrying can't succeed / would burn goodwill right after a "no").
`unknown`/`invalid_card` escalate at attempt 2, short-circuiting the 3-attempt runway.
`insufficient_funds` never uses `retry_now` — the money isn't there yet, so it's `retry_later`
from the first attempt.

### Checkout drop-off — `rule_based_dropoff_decision`

`abandonment_count` is windowed over `dropoff_lookback_days` (**7 days**).

| # | Condition | Action | Confidence |
|---|---|---|---|
| 1 | `checkout_status == "created"` | `no_action` | 0.90 |
| 2 | `amount >= high_value_amount_inr` | `escalate_to_human` | 0.85 |
| 3 | `abandonment_count <= 1` | `send_reminder` | 0.85 |
| 4 | `abandonment_count == 2`, incentive gates pass | `offer_incentive` | 0.80 |
| 5 | `abandonment_count == 2`, gates fail | `send_resume_link` | 0.75 |
| 6 | `abandonment_count >= 3` | `escalate_to_human` | 0.92 |

`created` means the customer never opened the payment screen — logged, never actioned. This
pipeline has no local `Customer` row (no tier), so its incentive check conservatively uses the
**lowest** tier's cap.

### Pre-checkout cart event — `rule_based_cart_event_decision`

Rules only — no LLM, no gate. Read as: **action when gates pass / action when they don't.**

| Tier | Explicit cancel | Silent abandon |
|---|---|---|
| **RISK** | `no_action` (0.85) | `send_reminder` (0.70) |
| **NEW** | `no_action` (0.75) | `send_reminder` (0.80) |
| **CASUAL** | `offer_incentive` (0.62) / `send_resume_link` (0.60) | `offer_incentive` (0.72) / `send_reminder` (0.78) |
| **REGULAR** | `offer_incentive` (0.65) / `send_resume_link` (0.62) | `offer_incentive` (0.77) / `send_reminder` (0.76) |
| **LOYAL** | `offer_incentive` (0.68) / `send_resume_link` (0.65) | `offer_incentive` (0.82) / `send_reminder` (0.75) |

**Checked before all of the above:** a 3rd+ explicit cancel within the behaviour window forces
`escalate_to_human` (0.88), overriding every tier including LOYAL. A silent abandon never
triggers it.

**The confidence numbers encode signal strength.** Every explicit-cancel branch scores lower
than the matching silent-abandon branch at the same tier — a silent abandon is ambiguous, an
explicit cancel is the customer actively saying no, so chasing it hard risks burning goodwill.
The consequence: every explicit-cancel incentive sits below the 0.70 bar, every silent-abandon
one sits above it.

### The `AgentAction` enum

Nine values, and the LLM can never return anything outside them:

| Group | Actions |
|---|---|
| Payment failure only | `retry_now`, `retry_later`, `send_nudge` |
| Drop-off + cart events | `send_reminder`, `send_resume_link`, `offer_incentive`, `no_action` |
| Shared by all pipelines | `escalate_to_human` |
| LLM-failure marker only | `rule_default_fallback` |

---

## Customer tiering

Tiers decide how much recovery effort a customer is worth — **not** payment success rate, which
graded customers on infrastructure they don't control and punished the ones who retried hardest
(`/checkout` mints a new order per retry, so FAILED, FAILED, CAPTURED scored 33%).

### Five tiers, evaluated in a fixed order

```
1. NEW      — zero purchase attempts. Checked first.
2. RISK     — enforcement gate. Checked BEFORE the score ladder.
3. CASUAL → REGULAR → LOYAL — the climbable ladder, by engagement score.
```

`NEW` and `RISK` are **states, not rungs**. RISK can't be reached by scoring badly, and a high
score can't rescue you out of it.

| Tier | Condition |
|---|---|
| `NEW` | 0 purchase attempts (only `CAPTURED`/`FAILED` orders count) |
| `RISK` | any of the three gates below trips |
| `LOYAL` | score ≥ **70** **and** ≥ **5** attempts |
| `REGULAR` | score ≥ **40** **and** ≥ **3** attempts |
| `CASUAL` | everything else with ≥ 1 attempt |

Score and volume are **AND**-ed — a single lucky order can't score its way to the top. Score 95
with 4 attempts is REGULAR, not LOYAL.

### The RISK gate — three independent paths

Each returns early, so the reason string names the first path that tripped.

| Path | Condition | Default |
|---|---|---|
| Permanent fraud flag | any prior order with `failure_reason == risk_block` | all-time, never expires |
| Attributable failure rate | `attributable_failed / attempts > rate`, ≥ min attempts | 0.60, min 2 |
| Cancel rate | `explicit_cancels / (cancels + successes) > rate`, ≥ min intents | 0.60, min 2 |

The failure-rate **denominator is all attempts** — including infrastructure-only failures — so a
network error dilutes the rate rather than driving it.

### The engagement score

A single 0–100 number from five weighted components, hardcoded on purpose:

```
score = round( 30·completion + 25·frequency + 20·monetary + 15·recency + 10·responsiveness )
```

| Component | Weight | Formula (each clamped to 0..1) | No-data case |
|---|---|---|---|
| **Completion** | 30 | `successes / (successes + attributable_failures + cancels + abandons)` | `0.0` |
| **Frequency** | 25 | `successes / (tenure_months × 1.0 target orders/mo)`, tenure floored at 1 month | `0.0` |
| **Monetary** | 20 | `avg_order_value / ₹2,000 target AOV` | `0.0` |
| **Recency** | 15 | `1 − (days_since_last_purchase / 90)` — linear, not exponential | `0.0` |
| **Responsiveness** | 10 | `nudges_resumed / (resumed + ignored)` | **`0.5`** |

Every component is a plain linear ratio with a clamp — no decay curves. Responsiveness defaults
to a neutral **0.5** rather than 0, so never having needed recovery isn't punished.

### Two things the score refuses to count

**Retries collapse into purchase attempts.** Orders group by an order-independent `sku:qty`
signature, and a `CAPTURED` order closes the run — `FAILED, FAILED, CAPTURED` is **one
successful attempt**, not a 33% success rate.

**Only customer-attributable failures count against anyone:**

| Attributable (counts) | Not attributable (excluded entirely) |
|---|---|
| `insufficient_funds`, `card_expired`, `invalid_card`, `authentication_failed`, `risk_block`, `cancelled` | `network_error`, `bank_decline`, `unknown` |

Excluded reasons are absent from the completion denominator, so an infrastructure failure
literally **cannot** lower the score.

### Windowing — different on purpose

| What | Window |
|---|---|
| Purchase attempts | all-time |
| Cart behaviour (cancels, abandons, nudge outcomes) | 180 days |
| Repeat-cancel escalation ladder | 180 days |
| Incentive frequency cap | 30 days |
| Drop-off abandonment count | 7 days |
| Recency decay | 90 days |
| `risk_block` fraud flag | never expires |

Behaviour is windowed so a customer who cancelled three carts once isn't penalised forever.
`tiering.py::refresh_tier` is the **only** place a tier is ever persisted.

---

## Incentive rules

The discount a customer sees is **fully deterministic** — the LLM is never on this path.

### The percentage is interpolated, not flat

```
position = clamp( (score − score_band_low) / (score_band_high − score_band_low) )
pct      = round( pct_low + position × (pct_high − pct_low) )
```

| Tier | Score band | Discount band | Worked example |
|---|---|---|---|
| CASUAL | 0–40 | **0–10%** | score 10 → position 0.25 → **2%** |
| REGULAR | 40–70 | **10–20%** | score 55 → position 0.5 → **15%** |
| LOYAL | 70–100 | **20–30%** | score 85 → position 0.5 → **25%** |

The score bands reuse the tier ladder's own thresholds, so retuning one retunes the other.

### Three independent gates, all of which must pass

| Gate | CASUAL | REGULAR | LOYAL | Direction |
|---|---|---|---|---|
| Max order value | ₹2,000 | ₹3,000 | ₹4,000 | scales **up** with tier |
| Max offers per 30 days | 3 | 2 | 1 | deliberately **inverted** |
| Tier eligibility | ✅ | ✅ | ✅ | NEW and RISK never |

**Frequency caps are inverted** so LOYAL (biggest discount) gets the fewest shots and CASUAL
(smallest discount) gets the most — otherwise the tier with the most room to exploit frequency,
discount, and cancel-tolerance together would get all three at once. Net effect: near-equal
discount exposure per customer per 30 days. **Every value cap sits below the high-value review
floor (₹5,000)**, so an incentive can never auto-apply where a failure would go to a human. All
three gates are stored as separate booleans on `CartEvent`, so the insights layer can tell which
one actually blocked an offer.

**NEW and RISK are hardcoded out, four times over** — an anti-abuse guardrail, not a tunable
economics parameter: `config.incentive_eligible_tier_set()` intersects against
`{casual, regular, loyal}`; the runtime live-mutation path applies the same intersection;
`rules_engine.py` re-checks tier membership; and the pct-band lookup returns `None` for them
regardless.

### Offers follow the cart

The pinned quantity is the **percentage**, not the amount — `/cart` and `/checkout` re-price
against the current cart on every read, so they can never disagree.

- **Cart edited above the tier cap** → discount **suspends** (`within_cap: false`). No state is
  stored about suspension; it snaps back automatically once under the cap again.
- **Attribution and discount are independent** — an over-cap purchase pays full price but still
  counts as a recovery.
- `incentive_final_amount_inr` means **terms actually redeemed**, set only at RESUMED time — a
  DECLINED offer keeps its stale proposal-time figure and never books `incentive_cost`.

### The repeat-cancel ladder

| Cancel # (within 180 days) | What happens |
|---|---|
| 1st / 2nd | Normal per-tier cancel branch — no distinct 2nd-cancel rung |
| **3rd+** | `escalate_to_human` (0.88), overriding every tier including LOYAL |

Escalated cancels get no resume card by design, so their at-risk money is closed out by the
maintenance sweep instead.

---

## Money: three at-risk buckets

`revenue.py` is a single-row ledger. Every mutation goes through one `adjust()` function, which
clamps buckets at 0 and writes a `revenue_state_updated` audit entry.

| Field | Meaning |
|---|---|
| `total_revenue` | Confirmed captured, all-time |
| `at_risk_soft` | Silent abandons / nudges — real recovery targets |
| `at_risk_declined` | Explicit cancels — tracked, weighted down |
| `at_risk_failed` | Failed payments — its own thread, its own exits |
| `total_recovered` | At-risk money that later converted |
| `total_lost` | Given up as unrecoverable |
| `incentive_cost` | Money given away via discounts |

Derived on read: `total_at_risk` and **`net_recovered = total_recovered − incentive_cost`**.

### The single-exit rule

**Three buckets, three threads — never crossed.** Every at-risk rupee must leave through exactly
one exit: **recovered** (a capture) or **lost** (declined, expired, superseded, given up, or
lapsed by the sweep) — which is why the maintenance sweep exists, to close every path that would
otherwise strand money "at risk" forever.

**At-risk is always booked at full value.** A discount is a cost paid later to recover the
money, never a reduction in what was at risk — it shows up separately as `incentive_cost` only
once redeemed.

### Exactly-once, twice over

| Flag | Guards | Why separate |
|---|---|---|
| `Order.revenue_recorded` | Capture-side money accounting | Both `/checkout/verify` and the `payment.captured` webhook can confirm the same capture. Claimed atomically by one conditional `UPDATE … WHERE revenue_recorded = false` — whoever wins does *all* the accounting. |
| `Order.risk_settled` | The failed-payment thread | An order that failed then captured must release its risk *and* book its revenue as two distinct claims — overloading one flag would break the same-order retry case. |

`order.status` alone must **never** gate money — a real past bug had `verify` flip status
without booking, so the webhook then saw `CAPTURED` and skipped booking too.

**How the failed-payment thread settles:** at-risk is booked once per purchase run (the run's
"carrier" is the first failure), released by exactly one of: any capture of the same basket
(→ `total_recovered`), `POST /checkout/give-up-failed` (→ `total_lost`, deliberately writes no
cart event to avoid double-tracking), or the maintenance sweep's lapse job.

**Cancel consolidation:** an explicit cancel releases all of the customer's open offers and live
attribution signals, books its own amount, and sends any shortfall (summed released amounts minus
the new cancel amount) to `total_lost` — the **sum**, not the max, so multiple consolidating
threads can't drop rupees from the ledger.

---

## Audit ledger

Every state change routes through one `write_audit_entry()` call into an append-only table. Rows
are never updated or deleted.

```
entry_hash = sha256( prev_hash | sequence_num | action_type | details | timestamp )
```

The first entry chains from a genesis hash of 64 zeros. **100+ distinct action types** are written
across the codebase — webhook receipt/rejection, decisions, executed actions, LLM fallbacks,
cart-event transitions, revenue mutations, tier changes, policy recommendations, scheduled jobs, messages, calls, promises, mandates,
links and maintenance sweeps. `revenue_state_updated` fires on every single ledger mutation, so money and audit log
reconcile against each other. The log is also read back, not just written: recovery analysis
reads `payment_failure_given_up` entries, since "gave up" vs. "silently lapsed" are otherwise
indistinguishable from the `Order` row alone.

**What `GET /audit/verify` actually proves:** it walks the chain and confirms each entry's
`prev_hash` matches the previous entry's `entry_hash` — nothing deleted, reordered, or spliced
in. That's chain-of-custody. It does **not** re-verify each entry's own content hash, so in-place
tampering with a single row's contents wouldn't be caught — see [Limitations and roadmap](#limitations-and-roadmap).

---

## Autopay, promises and receivables

These three features run on the **durable scheduler** (`scheduler.py`): every time-based action is a `ScheduledAction` row claimed with an atomic
`UPDATE … WHERE status='scheduled'` (exactly once). Customer-facing job kinds run only **08:00–19:00 IST** — a job due outside the window is
deferred, not dropped. A kind with no registered handler is skipped and audited.

### Card autopay (`autopay.py`, `mandate.py`)

Razorpay holds the card token, sends the bank pre-debit notice and moves the money; **our scheduler decides when**. Nothing charges behind our
back because Razorpay does not auto-retry orders created with a notification.

```
authorise on Razorpay  →  MANDATE_PDN job creates the debit order with a notification (≥ 37 h before the debit)
                       →  MANDATE_DEBIT job re-checks (mandate active, run unsettled, no paid link) then charges
failed debit           →  Event for the token → a MandateRun opens (money: at_risk_mandate)
                       →  retry ≥ 25 h out with a notice ≥ 24 h earlier · a call ~18 h before the retry · a promise or settle cancels them
dead mandate           →  re-mandate link;  runs that exhaust their retries or window lapse to "lost" via the sweep
```

- Every retry is ≥ 25 h out and has a notice ≥ 24 h earlier — there is no "skip the notice" path.
- Razorpay requires **36 hours between the notice and the debit, in real time**: a debit attempted earlier is refused, and `autopay._is_timing_refusal` re-queues it.
- `subscription.charged` is handled before `Event` creation (good news; books no revenue — `payment.captured` does).
- The ₹1 card authorisation is audited, not booked as revenue. `token.cancelled/paused` stop all debits.

### Promise to pay (`promises.py`)

A status overlay on an at-risk debt — **it never moves money**. Opening a promise pauses chasing and extends the debt's open payment link
(`PATCH /payment_links/{id}`, same id, no new link — capped at six months from creation). Defaults: consumer ≤ 7 days, business ≤ 30,
one-day grace, two broken promises then a person. Dates are judged only by `rules_engine.validate_promise`, whatever the source (voice, the
self-serve page, a merchant). Subjects plug in through `register_subject/opened_hook/broken_hook`.

### B2B receivables (`receivables.py`)

Invoices carry a Razorpay Payment Link (created lazily with the first chase message, reused, cancelled on paid / write-off / credit note /
dispute). The chase ladder fires at day offsets `-3, 1, 7, 15` from the due date and escalates to finance at day 30 — all settings.
Part-payments keep the same link. **Invoices are never auto-written-off** — a person decides. Money lives in separate
`receivables_overdue / collected / written_off` buckets that are not part of the consumer recovery rate. After a broken promise the chase
resumes "one step firmer", measured from the invoice's age on the ladder.

### Contacts, consent and messaging (`contacts.py`, `otp.py`, `dnd.py`, `messaging.py`)

- `ContactPoint` is the single source of truth for "may we call this person": an **OTP-verified phone** *and* consent the customer ticked
  (`consent_source == "customer_ticked"` is the only value). Merchant endpoints can set a number and language, never consent.
- **`messaging.py` is the only place a customer text leaves the system.** Templates are fixed (`TEMPLATE_VARS` fixes the variable order a DLT
  template must declare); rendering never involves the LLM. Every message is a row with an honest status: `recorded` (simulated), `sent`,
  `failed`, `not_sent_no_contact`. A kind with no template id is `failed`, never free text.
- Providers: `OTP_PROVIDER` and `MESSAGING_PROVIDER` are `simulated` or `msg91`; any other value is refused rather than falling back.
- Links are labelled by kind: `self_serve`, `razorpay_payment_link`, `storefront`, or `placeholder` (a stand-in shown as such).

---

## The Hinglish voice agent

One generic `place_call(subject_type, id)` with adapters for invoices, failed autopay debits and failed storefront orders.

**Guardrails (hardcoded):** verified number + customer-ticked consent · 08:00–19:00 IST · one call a day · attempt cap · DND registry check.
Consumer calls are for failed payments only (never abandoned carts) above a minimum value. A "dispute" on a mandate call is an
unauthorised-debit claim: automatic attempts stop and it goes to a human.

**Conversation (`voice_chat.py`, `persona.py`).** Multi-turn: the customer may ask (amount / why / due / how to pay / who / partial / repeat)
before deciding. One model call per turn returns intent, promise date and a candidate reply; **code then validates the reply** before it is
spoken — only real amounts and dates, no fee/interest/waiver/legal/threat wording, no spelled big numbers, no human claim, no links, the right
language, ≤ 320 characters. Any failure → the fixed template for that question. The opening always says "AI assistant … may be recorded", and
"are you a robot?" is answered honestly. Hardship gets one acknowledgement and "give a date or a person" instead of an escalation.

**Understanding speech.**
- *Dates* are read by a deterministic Hinglish/Devanagari reader (`voice.parse_promise_date`); when the model's date disagrees with the
  words, the words win and the agent confirms ("shall I note 12 Oct?"). No date → it asks, twice, then hands to a person.
- *Short replies* (`short_replies.py`): whole-utterance word lists for yes / no / later / wait / repeat / acknowledgement / "send it", in
  Roman Hinglish, English and Devanagari, tolerant of stretched spellings ("nahhh", "haaan"). A bare first "no" is checked once, never an
  instant refusal. The browser VAD keeps 250 ms of speech so a quick "haan" isn't dropped.
- *Hands-free listening*: Silero VAD in the browser, barge-in (the agent stops when interrupted), Whisper STT with the merchant and invoice
  words as bias; clips under 0.3 s, near-silent or known phantom phrases are dropped server-side.

**Knowledge and memory — two stores, one search surface** (`retrieval.py`, hybrid vector + full-text with reciprocal-rank fusion):

| Store | Owner | Holds | Rules |
|---|---|---|---|
| `knowledge_*` | Merchant | Approved policy passages (versioned; Hinglish/Hindi wording per passage) | Shared; spoken as fact only when retrieval is confident; `Also asked as:` aliases are searched, never spoken |
| `customer_memory`, `customer_facts`, `conversation_episodes`, `episode_chunks`, `memory_links` | One customer | Tiered memory: working (last turns) · factual (our own records) · semantic (allow-listed facts, superseded not overwritten) · episodic (call summary + the conversation word for word) | Needs the customer's memory consent *at the current wording*; expiring; deleted together on consent withdrawal, phone change or request |

A policy answer must cite approved passages and pass the reply checker, or the approved text itself is read ("As per our policy…") in the
call's language; nothing relevant → hand-off. A memory answer must cite retrieved lines or our own stored summary is read — never a guess.
The knowledge and memory stores are separate on purpose: different owner, trust and lifecycle, and mixing them could leak one customer into
another's answer. Every row carries a `merchant_id`.

**Speech (`speech.py`).** TTS = Azure AI Speech (neural voice, optional), STT = Groq Whisper; both fall back to the browser's own. Keys stay in
`.env`, travel in headers, and no endpoint returns them. The telephone line itself is not built — the browser is the phone.

---

## Policy analytics: four self-tuning analyses

Each analysis is a **pure aggregation module** (no LLM, no writes) + an **LLM module** + a shared router. Each aggregator also produces a
deterministic `patterns` list in Python, so the wording is controlled and survives the model being unreachable. If the model call fails,
`recommendations` is empty and `llm_error` is set — **a recommendation is never fabricated**. The four analysis modules share one retry for
Groq's provider-side errors (a malformed tool call, or a free-tier rate limit with a short wait).

| Analysis | Aggregator | What it measures | Tunable settings it may recommend |
|---|---|---|---|
| **Incentive economics** | `insights.py` | Redemption, incentive lift vs plain reminders, discount spend, net recovered per tier, which gate blocked offers | per-tier value caps, discount bands, 30-day frequency caps, `nudge_expiry_hours`, `casual_tier_incentive_eligible` |
| **Loss & recovery** | `recovery_insights.py` | The whole funnel in one shape (silent abandon, explicit cancel, payment failure, give-up), retry effectiveness, agent reliability | `confidence_threshold`, `max_auto_retries`, `high_value_amount_inr` |
| **Tier model** | `tier_insights.py` | Tier distribution, per-tier net gain, score distribution, near-miss customers, risk-flag redemption | score thresholds, attempt floors, calibration targets, RISK-gate params |
| **Collections** | `collections_insights.py` | Failed-debit recovery, promises, calls, invoices and payment links (below) | `mandate_max_retries`, `mandate_retry_window_days`, `promise_max_days_b2c/_b2b`, `promise_grace_days`, `promise_max_broken`, `voice_max_attempts_per_subject`, `receivables_escalate_after_days` |

**Applying a recommendation** (`POST /insights/apply-suggestion`) validates against the union of the whitelists, casts and range-checks the
value, then runs ordering guards that reject any change which would invert a ladder boundary (a regular-tier score at or above loyal, a lower
tier's discount band reaching the next tier's floor, grace > horizon, …). It mutates `runtime_flags` and audits before/after. **Tier settings
get a second safety step:** applying one changes how tiers are *computed* but re-evaluates nobody — the merchant previews exactly who would
move (`/insights/tier-reevaluation-preview`, read-only) and then commits.

### What the Collections analysis measures

*Setting-paired metrics* — each tunable setting is paired with the metric that is its direct evidence, and a recommendation is **dropped in
code** when that metric has fewer than `collections_min_sample` (default 20) resolved cases (the prompt is not trusted to enforce this):

| Setting | Evidence metric |
|---|---|
| `mandate_max_retries` | Recovery rate per retry number (settled runs only) |
| `mandate_retry_window_days` | Days from first failure to recovery (median, p90) |
| `promise_max_days_b2c` / `_b2b` | Kept rate by how far ahead the promise was made, consumer vs business |
| `promise_grace_days` | Broken promises that were paid within 1–2 days after the date |
| `promise_max_broken` | Kept rate of a debt's 1st / 2nd / 3rd promise |
| `voice_max_attempts_per_subject` | Productive-call rate by call number |
| `receivables_escalate_after_days` | Days past due when overdue invoices actually get paid |

*Supporting context* — no setting of their own; they explain *why* the numbers look as they do and feed the observations and the model's
reasoning (a pattern needs at least 5 cases in its own group):

| Metric | Question it answers |
|---|---|
| Failure reasons | Which reasons recover at all? (A retry fixes "insufficient funds", not an expired card or a revoked mandate.) |
| Overdue-invoice outcomes | What became of *every* overdue invoice — paid, open (by age), disputed, written off, in rupees — not just the ones that got paid? |
| Reminder-step effectiveness | Which chase step is followed by payment within 3 days? (last touch gets the credit) |
| Payment-link conversion | Paid vs expired, and how long payers took (real links only) |
| Promises by source | Voice vs self-serve link vs a person: which promises are kept? |
| How calls end | Promise, link, callback, no answer, handed to a person |
| Trend | This window against the equal window before it |

Not measured, on purpose: whether *healthy* recurring debits succeed (that outcome isn't stored per debit) and the calling window (a
hardcoded guardrail, not a setting).

---

## Razorpay integration

`razorpay_client.py` is the one place Payment Link / recurring calls go (the storefront and drop-off poller call `/v1/orders` inline). It has
typed errors, refuses an `rzp_live_` key outside production and an `rzp_test_` key in production, and exposes a single `set_transport` seam
that tests use to swap Razorpay for an `httpx.MockTransport`.

| Debt | Razorpay object | Confirmed by |
|---|---|---|
| Overdue invoice | Payment Link (part-payments allowed), reused while the amount owed and offer match, cancelled on paid / write-off / credit note / dispute; a promise **extends its expiry in place** | `payment_link.*`, `payment.captured` |
| Failed storefront order | Payment Link for the exact basket, showing an offer the cart *already had* (the link never outlives it); at most one reminder on the day the customer named; a store capture of the same basket cancels it | same; a link payment for an already-paid basket is flagged `duplicate_basket_payment` and refunded by a person |
| Failed autopay | Card mandate (token) on the recurring API, debited by our scheduler | `token.*`, `payment.captured/failed` |

- **Exactly-once money.** `payment_links.apply_payment` is reached by *both* `payment_link.paid/partially_paid` and `payment.captured`
  (whichever arrives first); payment-id dedupe on the row guarantees a payment books once.
- **Links are created lazily** — only when a message carrying one is about to go to a real number — because Test Mode caps a business at about
  30 links. At the cap, in stub mode, or on an error a **labelled stand-in** is used and audited (`payment_link_limit_reached`,
  `payment_link_creation_failed`); a message is never sent with no link and never with a fake one presented as real.
- **Webhooks** are deduplicated on `WebhookEventLog.razorpay_event_id` (the `X-Razorpay-Event-Id` header; `bodyhash:<sha256>` as a fallback).
- **Test Mode limits** (from Razorpay's documentation): no bank pre-debit notice is sent, a debit settles about five minutes after it is made,
  and the card decides the outcome — `4100 2800 0000 1007` succeeds, `4718 6000 0007 0021` fails with insufficient funds.
- `scripts/razorpay_probe.py` checks the account read-only and (with `--yes`) creates one link or mandate to answer open questions.

---

## Accounts and access

| Role | Signs in at `/login` as | Lands on | Can do |
|---|---|---|---|
| **Merchant** (exactly one) | "Merchant" | `/dashboard` | Everything merchant-side: dashboard, analyses, Simulation Console, register businesses and invoices, edit business contact details |
| **Business customer** | "My invoices" | `/account/invoices` | See their own invoices, pay (real Razorpay link), give or withhold call permission |
| **Autopay customer** (a shopper) | "My autopay" | `/account` | Pick a service or a custom amount, authorise on Razorpay, see the next debit, set permissions, cancel |
| **Shopper** | "Shop at the store" | `/store` | The storefront and its recovery experience |

Tokens carry a signed `role`; `decode_token` means "shopper token" so every storefront route rejects the others, and `require_role` guards the
rest. Customers can only reach their own mandates and invoices (tenant check on every query). The merchant account is created from
`MERCHANT_EMAIL` / `MERCHANT_PASSWORD` on first start (or `scripts/create_merchant.py`) and an existing password is never changed. Merchant
endpoints accept the merchant Bearer token or an `X-Merchant-Key` header (`MERCHANT_API_KEY`, opt-in); with neither configured the dashboard
stays open so a fresh clone still demos. Call permission is asked at the first reminder (self-serve link), on the business's own page, and
for autopay right after the mandate confirms — never at registration, never a condition of paying.

---

## Simulation Console

`/demo-console` (merchant only; returns 404 in production) lets you watch any debt's whole life without waiting for real time or spending real
vendor calls. The backend runs **exactly as it would live**; only the console adds switches.

- **Pick anything**: an invoice, an authorised autopay, a failed autopay debit, a failed order, or one of seven prepared test customers
  (no history, returning with a kept promise, broken promise + hardship, old consent wording, no memory consent, unverified phone, failed autopay).
- **Next event ▶** moves the shared demo clock to that debt's next queued job (into the calling window if needed) and runs it through the real
  scheduler. The queue shows what is confirmed next, what each step will do, and — for an autopay — what happens *after* the debit for each
  result (steps that depend on a result can only be queued once it exists).
- **SMS inbox** shows every notice, link and OTP; **Place call** rings in the browser and you answer as the customer (speech or typing).
- **Customer never pays** / **Promise was broken** play a debt to the end with stand-in links and unanswered calls (no real Razorpay link or
  debit); **Play: debit succeeds/fails** feeds a debit result through the normal webhook handler, labelled as played.
- **Real links** are off by default (Test Mode caps them); a switch turns them on. "Check payment now" / "Check Razorpay now" pull state without a
  webhook tunnel.
- The demo clock (`clock.py`) is in memory, forward-only, **shared by every debt**, refused in production, and affects *scheduling decisions
  only* (row timestamps and the audit log keep wall-clock time).

---

## Testing and evaluation

```bash
python -m pytest tests -q                 # 38 pure-logic unit tests
python -m evals.run                       # 13 offline suites (~890 cases), ~20 s, against a throwaway `rr_eval` database
python -m evals.run --live                # also the suite that calls the real model (Groq free tier: ~3 turns a minute)
python -m evals.selftest                  # 20 deliberate breakages the suites MUST catch
```

The eval pipeline never touches your real data (it refuses any database not named `rr_eval`) and writes `evals/reports/<time>.{json,md}`. A
suite fails if any *critical* case fails (a safety rule or an already-fixed bug) or a metric misses its bar. **`selftest` proves the suites are
not decorative**: each mutation breaks the app in one specific way (an expired-card retry, a double-booked link payment, a consent check
ignored, a customer reading another's mandate, …) and the named suite must go red.

| Suite | What it proves |
|---|---|
| `guardrails` | The reply checker blocks every unsafe or off-language reply (invented amounts/fees/waivers, threats, "I am human", links, Hinglish in an English call) and passes good ones |
| `understanding` | The keyword reader (the model-down fallback) gets decisions, questions, yes/no (incl. Devanagari), dates and short replies right; fixed bugs are pinned |
| `fixed_lines` | Every pre-written line exists in all three languages, formats, discloses "AI / may be recorded", says nothing forbidden |
| `policy_integrity` | `data/store_policy.md` matches the live settings, is fully translated, exposes no merchant notes |
| `retrieval` | Policy-search recall@k and rejection of off-topic questions per language, with keyword-only / vector-only ablations |
| `memory_write` / `memory_read` | Verbatim storage, consent-gated writes, superseded facts, derived links; profile card, recall cues, ranking, expiry |
| `privacy` | Zero cross-customer leakage; forget / consent withdrawal / phone change delete every tier |
| `flow` | The real call loop with a scripted model: promises, disputes, hardship, ask-for-date, short replies, grounded answers, outage fallback |
| `personas` | Each test customer is in the state it claims; Reset restores it |
| `razorpay_money` | Links and mandates against a fake Razorpay: exactly-once money, promise extends a link, debit never fires on a settled run |
| `accounts_console` | Role gates, tenant isolation, consent rules, console items, no real spend from the run-to-the-end buttons |
| `collections_analysis` | Every Collections metric on known data; no pattern from tiny groups; the seed data gives every setting enough evidence |
| `live_model` *(opt-in)* | The real model: intent accuracy vs the keyword reader, grounded multilingual answers, whole calls |

Known limits: the datasets are small and written in-house; bars were calibrated from a first baseline (they catch regressions, they don't prove
absolute quality); audio quality, speech-recognition accuracy and latency under load are not covered. See `evals/README.md`.

---

## Getting started

### Prerequisites

| Need | Version | Install |
|---|---|---|
| Python | 3.12+ | macOS: `brew install python@3.12` |
| Node | `^20.19` or `>=22.12` | [nodejs.org](https://nodejs.org) |
| PostgreSQL | 14+ | macOS: `brew install postgresql@16 && brew services start postgresql@16` |

### Quick start: run it on your computer (about 10 minutes)

1. **Install the prerequisites** above (Python 3.12+, Node 20.19+ or 22.12+, PostgreSQL 14+) and make sure Postgres is running.
2. **Get the code and install:**
   ```bash
   git clone https://github.com/Lakshaykhatri231/Backstop.git && cd Backstop
   python3 -m venv venv && source venv/bin/activate          # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   cd frontend && npm install && npm run build && cd ..       # the build is required: FastAPI serves frontend/dist
   ```
3. **Create the database and your `.env`:**
   ```bash
   createdb revenue_recovery
   cp .env.example .env
   ```
   Then edit `.env`. The **minimum** is these three lines — everything else has a default:
   ```bash
   DATABASE_URL=postgresql://<your-mac-username>@localhost:5432/revenue_recovery   # see the connection table below
   MERCHANT_EMAIL=you@example.com                                                   # lets you sign in as the merchant
   MERCHANT_PASSWORD=choose-a-long-password                                         # at least 8 characters
   ```
   Optional, each unlocks more: `GROQ_API_KEY` (AI decisions; without it the rules engine decides), `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` /
   `RAZORPAY_WEBHOOK_SECRET` (real Test Mode checkout, links and autopay), `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` (neural voice).
4. **Start it (from the project root):**
   ```bash
   uvicorn app.main:app --reload
   ```
   Wait for `Application startup complete.` The tables and the merchant login are created on this first start. (The first run may take a
   moment longer: a small local embedding model for the knowledge search is downloaded once.)
5. **Open <http://localhost:8000/login>**, choose **Merchant**, and sign in with the email and password from step 3. You land on the dashboard.
   It will be empty on a fresh database — load sample data (next section) to see every screen working.

Optional but recommended once it runs: `python scripts/seed_demo_all.py` (sample data), `python -m evals.run` (13 test suites), `python -m pytest tests`.

### Install and run (reference)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
createdb revenue_recovery
cp .env.example .env                           # then edit: at least DATABASE_URL (and Razorpay / Groq keys, below)
cd frontend && npm install && npm run build && cd ..
uvicorn app.main:app --reload                  # run from the project root; http://localhost:8000
```

> The frontend build is not optional: FastAPI serves `frontend/dist`, and `app/main.py` mounts it at import time. Re-run `npm run build` after
> any frontend change — `uvicorn --reload` doesn't. Tables are created on startup (`Base.metadata.create_all`).

Your connection string depends on how Postgres was installed: Homebrew → `postgresql://<mac-username>@localhost:5432/revenue_recovery`
(no password); Ubuntu default → `postgresql://postgres:postgres@localhost:5432/revenue_recovery`; Docker → `…postgres:<password>@…`.

The app degrades deliberately rather than crashing, so most of it works with no accounts at all: with no `GROQ_API_KEY` every decision falls back
to the rules engine; with no Razorpay keys everything works except real checkout and links (stand-ins are used); with no webhook secret,
webhooks are rejected rather than silently accepted.

### Load demo data (optional, one command)

```bash
python scripts/seed_demo_all.py                       # everything; safe to re-run; refuses ENVIRONMENT=production
python scripts/seed_demo_all.py --only collections    # or some steps (--skip policy leaves one out)
python scripts/reset_demo_data.py --yes [--then-seed] # DELETES everything in DATABASE_URL (schema kept), recreates the merchant login
```

| Step | Adds | Feeds |
|---|---|---|
| `storefront` | 5 named shoppers (`@demo.com`, password `password123`) with backdated history | the store demo |
| `analysis` | ~170 more shoppers with orders and cart events across every edge case | Incentive, Loss & Recovery, Tier analyses |
| `collections` | Settled failed-debit runs (with reasons), consumer and business promises, voice calls and how they end, paid *and* never-paid invoices, reminder history, real payment links | Collections analysis (all 8 settings clear the minimum sample) |
| `personas` | The 7 Simulation Console test customers | Simulation Console |
| `policy` | The approved store policy as the agent's knowledge | Policy answers on calls |
| `merchant` | The merchant login from `MERCHANT_EMAIL` / `MERCHANT_PASSWORD` | `/login` |

Back up first if the data matters: `pg_dump -Fc -d revenue_recovery -f backup.dump`.

### Sign in and look around

| URL | What |
|---|---|
| `/` | Landing page |
| `/login` | One sign-in page: store, autopay, invoices, merchant |
| `/dashboard` | Merchant console: Overview · Event Feed · Revenue & Customers · Receivables & Promises · Call Knowledge · Audit Log (the four analyses) |
| `/invoices` | Merchant-only: register businesses and invoices, edit contact details |
| `/demo-console` | Simulation Console (merchant only) |
| `/store` · `/account` · `/account/invoices` | Storefront · customer autopay · business invoices |
| `/architecture` · `/tiers` | Live walkthroughs that pull their numbers from the running system |
| `/docs` | Interactive API docs |

Merchant sign-in is whatever you set in `.env` (`MERCHANT_EMAIL` / `MERCHANT_PASSWORD`). Seeded shoppers use `password123`. OTP codes are
simulated: the page shows the code ("Demo mode, your code is …").

**Try a few things**

```bash
python scripts/simulate_webhook.py insufficient_funds   # HMAC-signed locally, no tunnel needed (also: risk_block, high_value, card_expired, halted, pending, charged)
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":true}'   # watch the rules-engine fallback
curl localhost:8000/audit/verify                        # {"chain_intact": true, ...}
curl localhost:8000/merchant/revenue                    # the money ledger
```

Seeded accounts trip different branches of the cart ladder: `priya@demo.com` (trusted) → offer; `rahul@demo.com` (new) → reminder; `neha@demo.com`
(risk-flagged) → no action; `zara@demo.com` (three prior cancels) → escalation.

### Real Razorpay Test Mode

1. Dashboard → **Test Mode** → API Keys → put `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in `.env`. Invent a `RAZORPAY_WEBHOOK_SECRET`.
2. Pay with card `4111 1111 1111 1111` (any future expiry, any CVV, **Success** on the bank page). For card autopay use
   `5305 6200 0006 0000` (Mastercard) to authorise.
3. To receive real webhooks, expose the server with a tunnel (the project uses **zrok**) and register
   `https://<your-share>/webhooks/razorpay` in Razorpay → Webhooks with the same secret and these events: `payment.failed`,
   `payment.captured`, `payment_link.paid`, `payment_link.partially_paid`, `payment_link.expired`, `payment_link.cancelled`,
   `token.confirmed`, `token.rejected`, `token.cancelled`, `token.paused`. Without a tunnel, use the console's "Check payment now" /
   "Check Razorpay now". If events stop arriving, check that the tunnel is up and that the webhook is still *Enabled* in the dashboard.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `RuntimeError: … frontend/dist/assets does not exist` | `cd frontend && npm run build` |
| `connection to server failed` / `role "postgres" does not exist` | `DATABASE_URL` doesn't match your Postgres install |
| Every decision says `rules_engine_fallback` | No/invalid `GROQ_API_KEY`, or the failure toggle is on |
| `POST /checkout` returns 500 | Razorpay test keys missing |
| Webhook returns 401 | `RAZORPAY_WEBHOOK_SECRET` mismatch |
| No webhooks arriving | Tunnel down (`curl https://<share>/`), or the webhook is disabled in the Razorpay dashboard |
| Dashboard empty | Expected on a fresh DB — run `scripts/seed_demo_all.py` |

---

## Configuration

Settings come from `.env` (`config.py::Settings`); live overrides in `runtime_flags.py` last until restart. **Decision code reads thresholds
through `runtime_flags`, never `settings` directly**, otherwise applied recommendations would silently do nothing. See `.env.example` for the
full list; the essentials:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (required) |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Razorpay Test Mode keys and the webhook signing secret |
| `RAZORPAY_LINKS` | `auto` (real when keys exist) · `real` · `stub` (labelled stand-ins, nothing reaches Razorpay) |
| `GROQ_API_KEY`, `LLM_MODEL` | Bounded decisions, call understanding, policy analyses |
| `CONFIDENCE_THRESHOLD`, `HIGH_VALUE_AMOUNT_INR`, `MAX_AUTO_RETRIES` | The decision gate (0.70 / ₹5,000 / 3) |
| `INCENTIVE_MAX_ORDER_VALUE_{CASUAL,REGULAR,LOYAL}` | Per-tier discount caps (see `config.py` for the full set of tier knobs) |
| `MERCHANT_EMAIL`, `MERCHANT_PASSWORD`, `MERCHANT_API_KEY` | The merchant login; an optional shared key for scripts |
| `OTP_PROVIDER`, `MESSAGING_PROVIDER` | `simulated` or `msg91` (anything else is refused) |
| `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` | Optional neural voice; the browser's voice is used when unset |
| `ENVIRONMENT` | `production` refuses simulated providers, default secrets and empty keys |
| `STOREFRONT_SECRET_KEY` | Signs session tokens — change before deploying |

---

## Operations

- **Schema changes.** `create_all` only adds missing *tables*. Columns and enum values live in idempotent files under `sql/` (run them in
  date order after the first start of new code, twice is safe): `for f in sql/*.sql; do psql -d revenue_recovery -q -f $f; done`.
  There is no Alembic; this is a known gap (see roadmap).
- **Scripts** (`scripts/`): `seed_demo_all.py`, `reset_demo_data.py`, `create_merchant.py`, `simulate_webhook.py`, `razorpay_probe.py`,
  `live_check.py` (one real OTP to yourself), `bench_retrieval.py`, `eval_stt.py`.
- **Audit integrity.** `GET /audit/verify` walks the hash chain; `GET /audit/log` reads it.
- **Production switch.** `ENVIRONMENT=production` makes startup refuse the default `STOREFRONT_SECRET_KEY`, an empty `MERCHANT_API_KEY`, an
  empty `RAZORPAY_WEBHOOK_SECRET`, and the simulated OTP / messaging / voice / DND providers; `/demo/*` returns 404.
- **Going live with SMS.** Register a DLT entity, sender id and one template per message kind on MSG91, set the `MSG91_*` variables and
  `OTP_PROVIDER=msg91` / `MESSAGING_PROVIDER=msg91`, then `python scripts/live_check.py --yes` sends exactly one OTP to your own number. The
  MSG91 request shapes are mock-tested only until that succeeds.

---

## Security, privacy and compliance posture

- **Inbound webhooks**: HMAC-SHA256 signature verified before anything is read; bad signatures are audited and rejected; deduped on event id.
- **Authentication**: PBKDF2-HMAC-SHA256 passwords and a hand-rolled HMAC-signed bearer token (stdlib only, no passlib/PyJWT); roles are signed
  into the token and can never be self-chosen at registration; tenant checks on every customer query.
- **Consent first**: calls need customer-ticked consent and an OTP-verified number; memory needs separate consent at the current wording and is
  deletable in one transaction; consent and DND are separate protections.
- **Contact limits**: 08:00–19:00 IST, one call a day, an attempt cap, "AI assistant / may be recorded" disclosure, honest answer to "are you a robot?".
- **Money safety**: exactly-once booking, single-exit buckets, audit entry on every ledger mutation; at-risk always booked at full value.
- **Secrets**: keys live in `.env` (gitignored), travel in headers (never URLs), and no endpoint returns them.
- **Not legal advice**: RBI's rules on retries are silent; retries use a conservative ≥ 25 h spacing with a prior notice. DPDP implications of storing call transcripts word for word are a counsel decision. DLT template approval is required for real SMS in India.

---

## What is real and what is simulated

| Area | Status |
|---|---|
| Razorpay Orders, Checkout, Payment Links, card mandates, webhooks (Test Mode) | **Real** (verified against Razorpay Test Mode) |
| Decision pipeline, scheduler, ledger, audit chain, tiering, analyses | **Real** logic, exercised by the eval suites |
| LLM decisions, call understanding, policy analyses | **Real** (Groq); falls back to rules when unavailable |
| Speech (Azure TTS, Whisper STT) | **Real** when keys are set; else the browser's |
| SMS and OTP | **Simulated** (stored in an inbox); MSG91 adapter written from its public docs and tested only against a mock |
| Voice "phone calls" | **Simulated**: the browser is the phone; no telephony provider |
| DND registry | **Simulated** list |
| Bank pre-debit notices | Not sent in Test Mode; timing compliance is shown by the scheduler and evals |
| A recurring debit succeeding after the full real-time notice window | Built for it; **not yet observed end to end** (needs ~37 real hours) |

---

## Limitations and roadmap

**Known limitations**
- Test Mode only. A business is capped at about 30 Payment Links; whether cancelling one frees a slot is unconfirmed.
- One merchant account; no multi-tenant isolation beyond the per-merchant `merchant_id` on the knowledge/memory stores.
- UPI AutoPay can't be tested in Test Mode (cards only).
- `runtime_flags` overrides (applied recommendations) live in memory and don't survive a restart.
- `audit.verify_chain` checks hash *linkage*; it doesn't yet recompute each entry's own content hash, so in-place tampering inside a single row
  wouldn't be caught (chain-of-custody is enforced, per-entry content integrity isn't).
- No migration framework (idempotent SQL files instead).
- The demo clock is shared: moving it for one debt runs other debts' due jobs.
- Groq's free tier (8,000 tokens/min) limits live calls to roughly three turns a minute.
- `voice_turns` transcripts are kept without a retention sweep.

**Roadmap**
- Real telephony + a real SMS carrier; DLT templates; a real DND lookup.
- Alembic migrations; persisted settings; multi-merchant tenancy.
- Content-hash verification in the audit chain.
- Measure healthy-debit success and judge an applied setting change before/after.
- Drafting the Hinglish/Hindi policy wording automatically at upload (merchant approves).

---

## Repository layout

```
app/                         FastAPI backend (~23k lines)
  main.py                      wiring, SPA routes, background threads
  webhook.py  dropoff.py  storefront.py           the three signal sources
  rules_engine.py  llm_agent.py  actions.py       deterministic decision · bounded refinement · execution stub
  tiering.py  revenue.py  audit.py  maintenance.py   score & tiers · money ledger · hash chain · sweeps
  scheduler.py  clock.py                          durable jobs · demo clock
  autopay.py  mandate.py  promises.py  receivables.py   recovery features on the scheduler
  payment_links.py  order_links.py  razorpay_client.py   Razorpay objects (one client module)
  contacts.py  otp.py  dnd.py  messaging.py  notify.py  selfserve.py   consent, verification, outbound text
  voice.py  voice_chat.py  persona.py  short_replies.py  speech.py     the call agent
  knowledge.py  retrieval.py  memory*.py  vectorstore.py  embeddings.py   knowledge + tiered memory
  insights*.py  recovery_insights*.py  tier_insights*.py  collections_insights*.py  groq_retry.py   analytics
  accounts.py  auth.py  merchant_auth.py  autopay_services.py             roles, portals, services
  demo.py  demo_text.py  demo_personas.py                                 Simulation Console
  models.py  rag_models.py  schemas.py  config.py  runtime_flags.py  database.py
frontend/                    React SPA: dashboard, storefront, portals, console, walkthroughs (~20k lines)
scripts/                     seeders, reset, webhook simulator, Razorpay probe, live checks
sql/                         idempotent column / enum migrations
evals/  tests/               13 offline eval suites + live suite + mutation self-test · unit tests
data/                        store_policy.md (the agent's approved knowledge) · sample_policy.md
```

Design rationale for each module lives in its own docstring and inline comments — worth reading before changing the logic they guard.
`CLAUDE.md` is a denser engineering reference to the same invariants.

---

## Author

Built by Lakshay Khatri.

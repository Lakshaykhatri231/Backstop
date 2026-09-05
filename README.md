# Backstop

**Razorpay Buildthon — Track 03: AI Revenue Recovery**

> *Find revenue that's slipping away and win it back.*

Backstop watches for revenue about to leak — a payment that fails, a checkout someone opens and
never finishes, a cart someone walks away from before checkout even starts — and runs each one
through the same bounded pipeline: a deterministic rules engine decides first, a language model
may only refine that decision within fixed limits, risky or low-confidence calls are handed to a
human, and every step is written to an append-only, hash-chained audit log. It ships with a real
Razorpay Test Mode storefront and a merchant dashboard, so the whole loop — detect, decide, act,
recover — is something a judge can actually click through, not just read about.

Live, interactive documentation of everything below is built into the app itself: run it and
visit `/architecture` (decision pipeline, money flow) and `/tiers` (customer tiering formula,
benefits) — both pull their numbers live from the running system, not a static diagram.

---

## How this meets the track's bar

> *"Don't just identify the problem. Show measured money recovered across a batch, with
> compliant escalation, stopping rules, and an audit trail."*

| Requirement | How Backstop does it | Where to see it |
|---|---|---|
| **Measured money recovered, across a batch** | A single-row revenue ledger (`total_revenue`, `total_recovered`, `total_lost`, `incentive_cost`, three separate at-risk buckets) updated on every state change via one `adjust()` function. Every rupee that enters an at-risk bucket leaves through exactly one exit — capture or loss. | `GET /merchant/revenue`, `GET /insights/recovery-analysis?range=30d`, Dashboard → Overview / Revenue & Customers |
| **Compliant escalation** | A confidence gate forces `escalate_to_human` below a configurable threshold; a high-value floor and a permanent `risk_block` flag escalate unconditionally. The LLM is schema-constrained to a fixed action enum — it can refine confidence and reasoning, but cannot invent an action or pick a money amount. | Dashboard → Overview → Human Gate Queue |
| **Stopping rules** | Max auto-retries before forced escalation; per-tier discount bands, order-value caps, and 30-day frequency caps (deliberately *inverted*); NEW and RISK customers hardcoded out of incentive eligibility; a 3rd+ cancel skips the offer entirely instead of escalating the discount. | `app/rules_engine.py`, `app/config.py`, `/tiers` |
| **Audit trail** | Every state change routes through one `write_audit_entry()` call into an append-only `audit_log` table, hash-chained via `prev_hash`. | `GET /audit/log`, `GET /audit/verify`, Dashboard → Audit Log |

---

## What's actually built

| Track direction | Status | Notes |
|---|---|---|
| Payment degradation → root cause → recovery action | **Built** | Razorpay webhook (`payment.failed`, `subscription.*`), HMAC-verified, normalized into an `Event`, decided by `rules_engine.py` against `FailureReason` and attempt count. |
| Checkout drop-off recovery | **Built** | No Razorpay webhook exists for "order created, never paid" — a background poller against the Orders API, actioning only orders stuck in `attempted`. |
| *(one step earlier)* Pre-checkout cart abandonment | **Built** | A cart that never became a Razorpay order — silent abandonment vs. explicit cancel are different signal strength, each with its own recovery ladder. |
| Failed-subscription recovery | **Partial** | `subscription.pending/halted/charged` webhooks ingested and decided by the same rules ladder — no subscription-specific win-back playbook beyond that yet. |
| B2B receivables chaser, mandate retry sequencer, Hinglish voice recovery, promise-to-pay tracker | **Not built** | Out of scope; the architecture is designed to extend to a fourth signal source the same way the third was added. |

---

## Architecture

### Three signal sources, one decision shape

Revenue leaks at three different points, and each needs a *different detection mechanism*:

| Signal | Why it needs its own detector | Entry point | Rules function | Rows written |
|---|---|---|---|---|
| **Payment failure** / subscription | Razorpay pushes it to us | `webhook.py` (HMAC-verified) | `rule_based_decision` | `Event` + `Decision` |
| **Checkout drop-off** | *Nothing fails*, so nothing fires — abandonment is the **absence** of a paid status, only findable by polling | `dropoff.py` (poller) | `rule_based_dropoff_decision` | `Event` + `Decision` |
| **Pre-checkout cart event** | No Razorpay order exists yet — nothing to receive *or* poll for | `storefront.py::_handle_cart_event` | `rule_based_cart_event_decision` | `CartEvent` only |

The third path is deliberately separate: an `Event` row only ever represents something Razorpay
knows about. `CartEvent` splits into `silent_abandon` vs `explicit_cancel` — different *signal
strength*, not just different labels. See **Decision pipeline** below for the shared sequence
all three feed into.

### Three background threads, started at boot

All three are load-bearing — each exists because some money would otherwise have no exit.

| Thread | Interval | What it does |
|---|---|---|
| `dropoff-poller` | 10 min | Queries Razorpay's Orders API for orders stuck in `attempted` past the abandonment window. |
| `cart-idle-sweep` | 5 min | Finds carts untouched 30+ minutes and fires the same silent-abandon pipeline the demo button fires manually. Open-ended query, not a trailing window. |
| `maintenance-sweep` | 30 min | Moves stale `CREATED` orders to `ABANDONED`; re-refreshes every tier so time-decaying score components don't go stale; closes dead recovery paths (expired offers, lapsed runs/signals) so no rupee is stranded "at risk" forever. |

### Where the LLM can and cannot act

| The model **may** | The model **cannot** |
|---|---|
| Adjust a decision's confidence score | Choose an action outside the `AgentAction` enum (blocked by tool schema) |
| Rewrite the human-readable reasoning | Pick or alter any money amount — discounts are a deterministic formula |
| Propose policy recommendations in the insights layer | Apply those recommendations itself, or touch a param outside the whitelist |
| — | Make NEW or RISK customers incentive-eligible (hardcoded out at four independent points) |

---

## Decision pipeline

Every signal follows the same sequence: **record event → deterministic rules decision → LLM
enrich (bounded) → confidence gate → `execute_action` → audit at every step.**

1. **Rules engine decides first.** `rules_engine.py` is the deterministic baseline — no LLM
   call ever happens inside it.
2. **The LLM may only refine.** `llm_agent.py` calls Groq with function-calling and an 8-second
   timeout, action constrained to the `AgentAction` enum by the tool schema.
3. **Any LLM failure falls back, loudly.** Timeout, error, or the `/debug/toggle-llm-failure`
   switch all return `source: "rules_engine_fallback"` and write an `llm_failure_fallback` audit
   entry.
4. **The confidence gate can override.** Below `confidence_threshold` (**0.70**), a decision is
   forced to `escalate_to_human`, flagged `escalated_by_confidence_gate`.
5. **Execution is a stub layer, on purpose.** `actions.py` makes no real retry/email/SMS call and
   **must never report "recovered."** Real recovery is only ever confirmed by a subsequent
   `payment.captured` webhook or an HMAC-verified checkout callback.

**One deliberate asymmetry:** the payment-failure pipeline exempts both `escalate_to_human` *and*
`retry_now` from the gate (gating `retry_now` would kill the unknown/invalid-card path); the
drop-off pipeline exempts only `escalate_to_human`. The cart-event path has no LLM layer and no
gate at all — its action is already bounded by the deterministic tier formula.

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

The first entry chains from a genesis hash of 64 zeros. **31 distinct action types** are written
across the codebase — webhook receipt/rejection, decisions, executed actions, LLM fallbacks,
cart-event transitions, revenue mutations, tier changes, policy recommendations, and maintenance
sweeps. `revenue_state_updated` fires on every single ledger mutation, so money and audit log
reconcile against each other. The log is also read back, not just written: recovery analysis
reads `payment_failure_given_up` entries, since "gave up" vs. "silently lapsed" are otherwise
indistinguishable from the `Order` row alone.

**What `GET /audit/verify` actually proves:** it walks the chain and confirms each entry's
`prev_hash` matches the previous entry's `entry_hash` — nothing deleted, reordered, or spliced
in. That's chain-of-custody. It does **not** re-verify each entry's own content hash, so in-place
tampering with a single row's contents wouldn't be caught — see *Honest limitations*.

---

## The self-tuning insights layer

Three parallel analyses read real aggregated data, propose config changes, and let a merchant
preview the exact customer-level impact before applying one. Each is a **pure aggregation
module** (no LLM, no writes) plus an **LLM module**, sharing one router:

| Analysis | Aggregator | What it measures | Whitelisted params |
|---|---|---|---|
| **Incentive economics** | `insights.py` | Redemption rates, incentive lift vs. plain reminders, discount spend, net recovered per tier, which gate blocked offers | 14: per-tier caps, discount bands, frequency caps, `nudge_expiry_hours`, `casual_tier_incentive_eligible` |
| **Loss & recovery** | `recovery_insights.py` | The whole funnel in one shape — silent abandon, explicit cancel, payment failure — plus give-up behaviour, retry effectiveness, agent reliability | 3: `confidence_threshold`, `max_auto_retries`, `high_value_amount_inr` |
| **Tier model** | `tier_insights.py` | Tier distribution, per-tier net gain, score distribution, near-miss customers, risk-flag redemption | 11: score thresholds, attempt floors, calibration targets, RISK-gate params |

A resumed cart that later fails at the gateway is reported as **handed to checkout**, not a cart
loss (its money already moved threads); a **give-up resolves a payment-failure run**, not a
fourth leak. Each aggregator also produces a deterministic `patterns` list in Python, so wording
stays controlled and survives the model being unreachable — **and if the LLM call itself fails,
`recommendations` is empty and `llm_error` is set, never a fabricated recommendation.**

**Applying a recommendation** (`POST /insights/apply-suggestion`): validates against the union
of the three whitelists, casts to the right type, range-checks (0–1 for rates, 0–100 for tier
scores, ≥1 for attempt floors), then runs **nine ordering guards** rejecting any change that
would invert a ladder boundary (e.g. regular score staying below loyal). Mutates `runtime_flags`
in memory and audits before/after values — changes take effect immediately but don't survive a
restart.

**Tier params get a second safety step:** applying a threshold changes how tiers are *computed*
but re-evaluates nobody. `GET /insights/tier-reevaluation-preview` shows exactly who would move
(read-only); `POST /insights/tier-reevaluation-commit` persists it through the same
`refresh_tier` hook other tier changes use.

---

## API surface

Merchant/dashboard endpoints are open by design (no auth); customer endpoints need
`Authorization: Bearer <token>`.

| Router | Endpoints | Auth |
|---|---|---|
| **Webhook** | `POST /webhooks/razorpay` | HMAC signature |
| **Auth** | `POST /auth/register`, `POST /auth/login` · `GET /auth/me` | open · Bearer |
| **Storefront** | `GET /catalog` · `GET /cart`, `POST /cart/add`, `POST /cart/remove`, `DELETE /cart`, `POST /cart/resume`, `POST /cart/decline-resume`, `GET /cart/pending-signals` | open · Bearer |
| **Checkout** | `POST /checkout`, `POST /checkout/verify`, `POST /checkout/give-up-failed` | Bearer |
| **Merchant** | `GET /merchant/revenue`, `/merchant/customers`, `/merchant/cart-events`, `/merchant/orders` | open |
| **Outcomes** | `GET /outcomes`, `GET /outcomes/events` | open |
| **Audit** | `GET /audit/log`, `GET /audit/verify` | open |
| **Insights** | `GET /insights/{incentive,recovery,tier}-analysis`, `GET /insights/tier-config`, `POST /insights/apply-suggestion`, `GET /insights/tier-reevaluation-preview`, `POST /insights/tier-reevaluation-commit` | open |
| **Debug** | `POST /debug/toggle-llm-failure`, `POST /debug/simulate-abandonment` · `POST /debug/simulate-cart-timeout` | open · Bearer |

---

## Tech stack

- **Backend**: FastAPI, SQLAlchemy 2.0, PostgreSQL, Pydantic Settings
- **Decision agent**: Groq's free-tier API over raw `httpx` with a function-calling tool schema —
  **no vendor SDK anywhere**, for either Groq or Razorpay
- **Payments**: Razorpay Test Mode — real Orders API, real Standard Checkout, real webhook
  signature verification
- **Auth**: stdlib only — PBKDF2-HMAC-SHA256 for passwords, a hand-rolled HMAC-signed bearer
  token. No passlib, no PyJWT
- **Frontend**: React 19, TanStack Router + Query, Vite, Tailwind 4, shadcn/radix — client-side
  SPA, served same-origin by FastAPI, so **no CORS middleware anywhere**, by design

---

## Setup

Roughly 10 minutes from clone to running, most of it waiting on `npm install`.

### 0. Prerequisites

| Need | Version | Install |
|---|---|---|
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) · macOS: `brew install python@3.12` |
| Node | `^20.19` or `>=22.12` | [nodejs.org](https://nodejs.org) · macOS: `brew install node` |
| PostgreSQL | 14+ | macOS: `brew install postgresql@16 && brew services start postgresql@16` · Ubuntu: `sudo apt install postgresql && sudo systemctl start postgresql` · [Windows installer](https://www.postgresql.org/download/windows/) |

```bash
python3 --version && node --version && psql --version
```

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..
```

> **The frontend build is not optional.** `app/main.py` mounts `frontend/dist/assets` at import
> time, so skipping `npm run build` fails with `RuntimeError: Directory ... does not exist`
> before the server starts. Re-run it after any frontend change — `uvicorn --reload` doesn't.

### 2. Create the database

```bash
createdb revenue_recovery          # tables auto-create on first server start
```

No migrations — `Base.metadata.create_all` builds the schema on startup. **Your connection
string depends on how Postgres was installed** — the single most common setup failure:

| Install | Connection string |
|---|---|
| macOS / Homebrew | `postgresql://<your-mac-username>@localhost:5432/revenue_recovery` (no password) |
| Ubuntu / Debian default | `postgresql://postgres:postgres@localhost:5432/revenue_recovery` |
| Docker `postgres` image | `postgresql://postgres:<your-password>@localhost:5432/revenue_recovery` |

Set it in `.env` as `DATABASE_URL`, and verify with `psql revenue_recovery -c '\conninfo'`.

### 3. Configure `.env`

```bash
cp .env.example .env
```

| Variable | Where to get it | Required? |
|---|---|---|
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | [Razorpay Dashboard](https://dashboard.razorpay.com) → **Test Mode** → Account & Settings → API Keys → Generate | For checkout |
| `RAZORPAY_WEBHOOK_SECRET` | **You invent this** — any string, must just match what you enter in Razorpay's webhook settings | For webhooks |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys, free, no card | Optional |
| `DATABASE_URL` | See the table above | Yes |

**The app degrades deliberately rather than crashing**, so most of it is evaluable with no
accounts at all: with no `GROQ_API_KEY`, every decision falls back to the rules engine
(`source: "rules_engine_fallback"`) — same path the failure-injection demo exercises on purpose.
With no Razorpay keys, everything works except `POST /checkout` (clear 500). With no webhook
secret, webhooks are rejected rather than silently accepted, and `simulate_webhook.py` refuses
to run.

### 4. Run it

```bash
# Always run from the project root - main.py resolves frontend/dist relatively.
uvicorn app.main:app --reload
```

Wait for `Application startup complete.` Three background threads start silently alongside it —
they only log once they actually do something, so no extra boot output is expected.

### 5. Confirm it's actually working

```bash
curl localhost:8000/merchant/revenue     # -> the ledger as JSON
curl localhost:8000/audit/verify         # -> {"chain_intact": true, ...}
```

| URL | What |
|---|---|
| `http://localhost:8000/` | Landing page |
| `http://localhost:8000/dashboard` | Merchant console (no auth) |
| `http://localhost:8000/store` | Demo storefront (real Razorpay checkout, sign in/register) |
| `http://localhost:8000/architecture` | Live decision-pipeline walkthrough |
| `http://localhost:8000/tiers` | Live customer-tiering walkthrough |
| `http://localhost:8000/login` · `/support` | Customer sign-in · support page |
| `http://localhost:8000/docs` | Interactive API docs |

Now jump to the **Demo walkthrough** below.

### Paying with a test card

Razorpay Test Mode never charges real money. Use card **4111 1111 1111 1111**, any future
expiry, any CVV, any name; choose **Success** on the simulated bank page. Full list of test
cards [here](https://razorpay.com/docs/payments/payments/test-card-details/).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `RuntimeError: ... frontend/dist/assets does not exist` | SPA wasn't built. `cd frontend && npm run build && cd ..` |
| `connection to server failed` / `role "postgres" does not exist` | `DATABASE_URL` doesn't match your Postgres install — see step 2's table. |
| `createdb: command not found` | Postgres isn't on `PATH`; start the service and re-open the shell. |
| `psycopg2` fails to build | Ubuntu: `sudo apt install libpq-dev python3-dev` first. |
| Dashboard is empty | Expected on a fresh DB — run the seed scripts below. |
| Every decision says `rules_engine_fallback` | No/invalid `GROQ_API_KEY`, or the failure toggle is on — `curl -X POST localhost:8000/debug/toggle-llm-failure -d '{"forced":false}'` |
| `POST /checkout` returns 500 | Razorpay test keys missing from `.env`. |
| Webhook returns 403 | `RAZORPAY_WEBHOOK_SECRET` mismatch. |
| Frontend changes don't show up | `uvicorn --reload` doesn't rebuild the SPA — re-run `npm run build`. |

### Receiving real Razorpay webhooks (optional)

Razorpay only delivers to a public URL, so a local server needs a tunnel — this project uses
**zrok**:

```bash
zrok enable <your-account-token>     # one-time, per machine
zrok share public localhost:8000     # prints a public HTTPS URL
```

Put that URL + `/webhooks/razorpay` into Razorpay Dashboard → Settings → Webhooks (select
`payment.failed`, `payment.captured`, `subscription.pending/halted/charged`), using the same
secret as `RAZORPAY_WEBHOOK_SECRET`. The URL changes each share, so re-paste it after restarting
the tunnel. Everything in the demo below works **without** a tunnel — the simulator signs its
own payloads locally.

---

## Demo walkthrough for judges

### 1. Seed the data (no server needed — writes straight to the DB)

```bash
python scripts/seed_storefront_customers.py   # 5 named accounts with backdated history
python scripts/seed_analysis_data.py          # synthetic customers across every analysis's edge cases
```

Log in at `/store` (password `password123` for all) and use the **"Try the recovery agent"**
panel. Each seeded account trips a *different* ladder branch:

| Account | State | Do this | Expected decision |
|---|---|---|---|
| `priya@demo.com` | trusted, long history | add item → **Simulate cart timeout** | `offer_incentive` |
| `rahul@demo.com` | new, no history | add item → **Simulate cart timeout** | `send_reminder`, no incentive |
| `neha@demo.com` | risk-flagged | add item → **Delete cart** | `no_action` |
| `zara@demo.com` | 3 prior cancels | **Delete cart** | `escalate_to_human` (4th cancel) |

Check `/dashboard` → Event Feed for the decision, confidence, and reasoning behind each.

### 2. Trigger a payment-failure scenario live (server running)

```bash
python scripts/simulate_webhook.py insufficient_funds
python scripts/simulate_webhook.py risk_block      # always escalates, no exceptions
python scripts/simulate_webhook.py high_value      # escalates on amount alone
python scripts/simulate_webhook.py card_expired
python scripts/simulate_webhook.py halted          # subscription.halted
```

The script HMAC-signs its own payload, so this works with no tunnel. Passing the same event id
twice exercises the **idempotency** path.

### 3. Prove the graceful-failure path

```bash
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":true}'
python scripts/simulate_webhook.py insufficient_funds
# response shows source: "rules_engine_fallback" - decision still made and executed
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":false}'
```

Then pull `/audit/log` and point at the `llm_failure_fallback` entry.

### 4. Verify the audit chain and walk the money

```bash
curl localhost:8000/audit/verify         # or Dashboard -> Audit Log -> Verify Chain
curl localhost:8000/merchant/revenue     # ledger snapshot
```

`/insights/recovery-analysis?range=all` breaks the whole funnel out by loss signal. The
dashboard's Overview and Revenue & Customers tabs show the same numbers, including the three
at-risk buckets separately.

### 5. Try a self-tuning suggestion

Dashboard → Audit Log → any analysis modal (📊 Incentive / 📉 Loss & Recovery / 🎯 Tier) →
**Analyze** → **Preview Impact** on a recommendation → **Apply**. Every step is audited. Tier
recommendations show exactly which customers would change tier before you commit.

---

## Project structure

```
app/                        FastAPI backend
  main.py                     app wiring, SPA routes, 3 background threads
  webhook.py                  Razorpay webhook -> Event/Decision (payment failures, subscriptions)
  dropoff.py                  background poller -> Event/Decision (checkout drop-off)
  storefront.py               cart/checkout, cart-event decisions, cart-idle sweep
  auth.py                     PBKDF2 passwords + HMAC bearer tokens (stdlib only)
  rules_engine.py             deterministic baseline for all three signal sources
  llm_agent.py                bounded Groq refinement layer (+ fallback)
  actions.py                  execution stub layer (never reports "recovered")
  revenue.py                  single-row money ledger, three at-risk buckets, exactly-once booking
  tiering.py                  engagement score, tier ladder, incentive % formula
  audit.py                    append-only, hash-chained audit log
  maintenance.py              tier refresh + stale-order + dead-recovery-path sweeps
  merchant.py, outcomes.py    dashboard read APIs
  insights*.py                the three analyses (aggregator + LLM each)
  insights_router.py          analysis endpoints, apply-suggestion, tier re-evaluation
  config.py, runtime_flags.py startup config vs. live in-memory overrides
  models.py, schemas.py, database.py
frontend/                   React SPA (dashboard, storefront, architecture & tiers walkthroughs)
scripts/                    seeders + a live-webhook simulator (see script docstrings)
```

Design rationale for each module lives in its own docstring and inline comments — worth reading
before changing the logic they guard. For the same material as live diagrams, run the app and
open `/architecture` and `/tiers`.

---

## Honest limitations

There's no automated test suite — verification is by running the server, replaying scenarios
through the seed/simulate scripts, and reading `/audit/log` / `/audit/verify`.

`actions.py` is a stub layer: no real retry/email/SMS call is ever made, and it never reports
"recovered" on its own — real recovery is only ever confirmed by a subsequent Razorpay
`payment.captured` webhook or an HMAC-verified checkout callback.

`audit.verify_chain` currently checks hash-linkage between entries but doesn't yet re-verify each
entry's own content hash, so within-entry tampering wouldn't be caught (chain-of-custody is
enforced; per-entry content integrity isn't, yet).

Policy changes applied through `/insights/apply-suggestion` live in memory only — they take
effect immediately but don't persist across a restart.

There is no database migration tooling: `Base.metadata.create_all` only adds missing *tables*, so
adding or renaming a column on an existing model needs a manual `ALTER` (or dropping the table).

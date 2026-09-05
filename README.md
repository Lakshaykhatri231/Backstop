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
| **Measured money recovered, across a batch** | A single-row revenue ledger (`total_revenue`, `total_recovered`, `total_lost`, `incentive_cost`, and three separate at-risk buckets) updated on every state change via one `adjust()` function. Every rupee that enters an at-risk bucket leaves through exactly one exit — capture or loss — so nothing double-counts or gets stranded. | `GET /merchant/revenue`, `GET /insights/recovery-analysis?range=30d`, Dashboard → Overview / Revenue & Customers |
| **Compliant escalation** | A confidence gate forces `escalate_to_human` below a configurable threshold; a high-value floor and a permanent `risk_block` flag escalate unconditionally, no exceptions. The LLM is schema-constrained (Groq function-calling) to a fixed action enum — it can refine confidence and reasoning, but it cannot invent an action or pick a money amount. | Dashboard → Overview → Human Gate Queue |
| **Stopping rules** | Max auto-retries before forced escalation; per-tier discount bands, order-value caps, and 30-day frequency caps (deliberately *inverted* — the tier with the biggest discount gets the fewest shots at it); NEW and RISK customers are hardcoded out of incentive eligibility regardless of config; a 3rd+ cancel skips the offer entirely instead of escalating the discount. | `app/rules_engine.py`, `app/config.py`, `/tiers` |
| **Audit trail** | Every state change — decision, action, revenue mutation, tier change, config change — routes through one `write_audit_entry()` call into an append-only `audit_log` table, hash-chained via `prev_hash`. | `GET /audit/log`, `GET /audit/verify`, Dashboard → Audit Log |

---

## What's actually built

The track lists several example directions; here's what Backstop covers and how, stated plainly
rather than oversold:

| Track direction | Status | Notes |
|---|---|---|
| Payment degradation → root cause → recovery action | **Built** | Razorpay webhook (`payment.failed`, `subscription.*`), HMAC-verified, normalized into an `Event`, decided by `rules_engine.py` against `FailureReason` (insufficient funds, card expired, bank decline, risk block, network error, auth failed, invalid card…) and attempt count. |
| Checkout drop-off recovery | **Built** | No Razorpay webhook exists for "order created, never paid" — Backstop runs its own background poller against the Orders API, actioning only orders stuck in `attempted`. |
| *(not in the track's list, but the same problem one step earlier)* Pre-checkout cart abandonment | **Built** | A cart that never became a Razorpay order at all — silent abandonment vs. an explicit cancel are treated as genuinely different signal strength, not just different labels, each with its own recovery ladder. |
| Failed-subscription recovery | **Partial** | `subscription.pending` / `subscription.halted` / `subscription.charged` webhooks are ingested and decided by the same rules ladder — there's no subscription-specific win-back playbook beyond that yet. |
| B2B receivables chaser, mandate retry sequencer, Hinglish voice recovery, promise-to-pay tracker | **Not built** | Out of scope for this build; the architecture (bounded rules → LLM refine → gate → audit) is designed to extend to a fourth signal source the same way the third one was added, but none of these four exist yet. |

---

## Architecture

### Three signal sources, one decision shape

Revenue leaks at three different points, and each needs a *different detection mechanism* —
which is the whole reason there are three entry points instead of one webhook handler:

| Signal | Why it needs its own detector | Entry point | Rules function | Rows written |
|---|---|---|---|---|
| **Payment failure** / subscription event | Razorpay pushes it to us | `webhook.py` (HMAC-verified) | `rule_based_decision` | `Event` + `Decision` |
| **Checkout drop-off** | *Nothing fails*, so nothing fires. Abandonment is the **absence** of a paid status — only findable by polling | `dropoff.py` (background poller) | `rule_based_dropoff_decision` | `Event` + `Decision` |
| **Pre-checkout cart event** | No Razorpay order exists yet, so there's nothing to receive *or* poll for | `storefront.py::_handle_cart_event` | `rule_based_cart_event_decision` | `CartEvent` only |

The third path is deliberately separate: an `Event` row only ever represents something Razorpay
knows about. `CartEvent` splits into `silent_abandon` vs `explicit_cancel`, treated as different
*signal strength*, not just different labels.

```
Payment failure/subscription event    Checkout drop-off               Pre-checkout cart event
   (Razorpay webhook, HMAC-verified)     (background poller)             (storefront, no order exists yet)
              |                                |                                  |
              v                                v                                  v
      rule_based_decision            rule_based_dropoff_decision      rule_based_cart_event_decision
              |                                |                                  |
              +---------------+----------------+                                  |
                              v                                                   |
                    llm_agent.py (Groq, 8s timeout,                               |
                    schema-constrained action enum)                       (rules only — no
                              |                                          Razorpay order yet,
                    -- success: use LLM's answer                          so no LLM layer)
                    -- failure: fall back to rules,                               |
                       audited as llm_failure_fallback                            |
                              |                                                   |
                              v                                                   |
                  confidence gate (may force                                      |
                  escalate_to_human; high-value                                    |
                  and risk_block always escalate)                                 |
                              |                                                   |
                              +-------------------+-------------------------------+
                                                  v
                                        execute_action() (stub layer —
                                        never reports "recovered"; real
                                        recovery is only ever confirmed
                                        by a later payment.captured webhook)
                                                  v
                          audit.py — append-only, hash-chained entry at every step
```

### Three background threads, started at boot

`app/main.py` starts three daemon threads on startup — all three are load-bearing, and each
exists because some money would otherwise have no exit:

| Thread | Interval | What it does |
|---|---|---|
| `dropoff-poller` | 10 min | Queries Razorpay's Orders API for orders stuck in `attempted` past the abandonment window, and runs each through the drop-off pipeline. |
| `cart-idle-sweep` | 5 min | Finds carts untouched for 30+ minutes and fires the same silent-abandon pipeline the demo button fires manually. Uses an *open-ended* query (not a trailing window), so a cart idle for 2 hours is as much a hit as one idle for 31 minutes. |
| `maintenance-sweep` | 30 min | Three jobs: moves stale `CREATED` orders to `ABANDONED`; re-refreshes every customer's tier so time-decaying score components don't go stale on dormant accounts; and closes dead recovery paths (expired offers, lapsed failed runs, lapsed attribution signals, lapsed escalated cancels) so no rupee is stranded "at risk" forever. |

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

1. **Rules engine decides first.** `rules_engine.py` is the deterministic baseline and stays
   independently readable and testable — no LLM call ever happens inside it.
2. **The LLM may only refine.** `llm_agent.py` calls Groq with function-calling and an 8-second
   timeout. The action is constrained to the `AgentAction` enum by the tool schema, so the model
   can confirm or adjust confidence and reasoning but cannot invent an action.
3. **Any LLM failure falls back, loudly.** Timeout, error, unreachable host, or the
   `/debug/toggle-llm-failure` switch all return `source: "rules_engine_fallback"` and write an
   `llm_failure_fallback` audit entry. Nothing crashes and nothing goes silent.
4. **The confidence gate can override.** Below `confidence_threshold` (**0.70**), a decision is
   forced to `ESCALATE_TO_HUMAN`, flagged `escalated_by_confidence_gate`, and its reasoning is
   prefixed `[Confidence gate override] Original decision was '<action>'…`.
5. **Execution is a stub layer, on purpose.** `actions.py` makes no real retry/email/SMS call
   and **must never report "recovered."** Real recovery is only ever confirmed by a subsequent
   `payment.captured` webhook or an HMAC-verified checkout callback.

**One deliberate asymmetry in the gate:** the payment-failure pipeline exempts both
`escalate_to_human` *and* `retry_now` from the gate — gating `retry_now` would kill the
unknown/invalid-card path, and it's the lowest-stakes action available. The drop-off pipeline
exempts only `escalate_to_human`. The cart-event path has no LLM layer and no gate at all,
because its action is already bounded by the deterministic tier formula.

---

## The rules engine, ladder by ladder

All thresholds below are read live through `runtime_flags`, so a change applied via
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
| 10 | `bank_decline` / `network_error` / `authentication_failed`, 1st attempt | `retry_now` | 0.75 network, else 0.65 |
| 11 | same three, 2nd+ attempt | `retry_later` | 0.50 |
| 12 | unrecognised reason code | `escalate_to_human` | 0.30 |

Why these branches are shaped this way:

- **The retry cap is checked first**, above even `risk_block` — attempts 1–3 proceed, attempt 4
  hard-escalates.
- **The high-value floor sits second**, above every reason-specific branch, so a ₹5,000
  `network_error` escalates rather than retrying. Its 0.60 confidence deliberately lands *below*
  the 0.70 bar.
- **`risk_block` never gets an attempt-1 retry.** Auto-retrying a risk-flagged payment is unsafe
  at any attempt count.
- **`card_expired` and `cancelled` are message-only.** Retrying literally cannot succeed on an
  expired card, and retrying right after an explicit "no" burns goodwill.
- **`unknown`/`invalid_card` escalate at attempt 2**, short-circuiting the 3-attempt runway —
  two unclassifiable failures in a row is a stronger signal than one.
- **`insufficient_funds` never uses `retry_now`.** The money genuinely isn't there yet, so it's
  `retry_later` from the first attempt; only the confidence decays.

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

The `created` check comes first because it means the customer never opened the payment screen —
a weaker signal that gets logged for metrics but never actioned. This pipeline only has
Razorpay's customer id (no local `Customer` row, so no tier), so its incentive check
conservatively uses the **lowest** tier's cap.

### Pre-checkout cart event — `rule_based_cart_event_decision`

Rules only — no LLM, no gate. Read as: **action when the incentive gates pass / action when they
don't.**

| Tier | Explicit cancel | Silent abandon |
|---|---|---|
| **RISK** | `no_action` (0.85) | `send_reminder` (0.70) |
| **NEW** | `no_action` (0.75) | `send_reminder` (0.80) |
| **CASUAL** | `offer_incentive` (0.62) / `send_resume_link` (0.60) | `offer_incentive` (0.72) / `send_reminder` (0.78) |
| **REGULAR** | `offer_incentive` (0.65) / `send_resume_link` (0.62) | `offer_incentive` (0.77) / `send_reminder` (0.76) |
| **LOYAL** | `offer_incentive` (0.68) / `send_resume_link` (0.65) | `offer_incentive` (0.82) / `send_reminder` (0.75) |

**Checked before all of the above:** a 3rd+ explicit cancel within the behaviour window forces
`escalate_to_human` (0.88), overriding *every* tier including LOYAL. A silent abandon never
triggers it, no matter how many the customer has.

**The confidence numbers encode signal strength, deliberately.** Every explicit-cancel branch
scores *lower* than the matching silent-abandon branch for the same tier (0.68 / 0.65 / 0.62 vs
0.82 / 0.77 / 0.72). A silent abandon is ambiguous — maybe the phone rang. An explicit cancel is
the customer actively saying no, and chasing that with a hard sell risks burning goodwill. The
consequence is that every explicit-cancel incentive sits below the 0.70 bar while every
silent-abandon one sits above it.

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

Tiers decide how much recovery effort a customer is worth. They are **not** based on payment
success rate — that model was removed because it graded customers on infrastructure they don't
control, and actively punished the customers who retried hardest (`/checkout` mints a new order
per retry, so FAILED, FAILED, CAPTURED scored as a 33% success rate).

### Five tiers, evaluated in a fixed order

```
1. NEW      — zero purchase attempts. Checked first.
2. RISK     — enforcement gate. Checked BEFORE the score ladder.
3. CASUAL → REGULAR → LOYAL — the climbable ladder, by engagement score.
```

`NEW` and `RISK` are **states, not rungs**. Because RISK is checked before the ladder, it can't
be reached by merely scoring badly — and a high score can't rescue you out of it.

| Tier | Condition |
|---|---|
| `NEW` | 0 purchase attempts (only `CAPTURED`/`FAILED` orders count; cart events don't move you off it) |
| `RISK` | any of the three gates below trips |
| `LOYAL` | score ≥ **70** **and** ≥ **5** attempts |
| `REGULAR` | score ≥ **40** **and** ≥ **3** attempts |
| `CASUAL` | everything else with ≥ 1 attempt |

Score and volume are **AND**-ed. The volume floors exist so one lucky order can't score its way
to the top — a single successful first order otherwise scores near-perfectly on every
ratio-based component at once. So a customer with **score 95 and 4 attempts is REGULAR, not
LOYAL**.

### The RISK gate — three independent paths

Each returns early, so the reason string names the first path that tripped.

| Path | Condition | Default |
|---|---|---|
| Permanent fraud flag | any prior order with `failure_reason == risk_block` | all-time, never expires |
| Attributable failure rate | `attributable_failed / attempts > rate`, with ≥ min attempts | 0.60, min 2 |
| Cancel rate | `explicit_cancels / (cancels + successes) > rate`, with ≥ min intents | 0.60, min 2 |

The failure-rate **denominator is all attempts** — including infrastructure-only failures — so a
network error dilutes the rate rather than driving it.

### The engagement score

A single 0–100 number from five weighted components. The weights are **hardcoded on purpose** —
they're an internal scoring detail, not a merchant-tunable knob:

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

Every component is a plain linear ratio with a clamp — there are no decay curves anywhere in
this model. Responsiveness is the one component that defaults to a neutral **0.5** rather than 0:
scoring someone 0 would punish a well-behaved customer for never having needed recovery.

### Two things the score deliberately refuses to count

**Retries are collapsed into purchase attempts.** Orders are grouped by an order-independent
`sku:qty` signature, and a `CAPTURED` order closes the run. So `FAILED, FAILED, CAPTURED` for the
same basket is **one successful attempt**, not a 33% success rate. Buying the same basket again
later starts a fresh run, so genuine repeat buying still counts separately.

**Only customer-attributable failures count against anyone.**

| Attributable (counts) | Not attributable (excluded entirely) |
|---|---|
| `insufficient_funds`, `card_expired`, `invalid_card`, `authentication_failed`, `risk_block`, `cancelled` | `network_error`, `bank_decline`, `unknown` |

Excluded reasons are absent from the completion denominator, so an infrastructure failure
literally **cannot** lower a customer's engagement score.

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

Behaviour counts are windowed so a customer who cancelled three carts once and then behaved
perfectly for a year isn't penalised forever.

`tiering.py::refresh_tier` is the **only** place a tier is ever persisted, which is what makes
`tier_changed` auditing complete.

---

## Incentive rules

The discount a customer sees is **fully deterministic** — the LLM is never on this path, and the
audit trail can always reproduce exactly why a given customer was offered a given number.

### The percentage is interpolated, not flat

Each tier has a **discount band**, and the customer's exact % inside it comes from where their
score sits inside their tier's **score band**:

```
position = clamp( (score − score_band_low) / (score_band_high − score_band_low) )
pct      = round( pct_low + position × (pct_high − pct_low) )
```

| Tier | Score band | Discount band | Worked example |
|---|---|---|---|
| CASUAL | 0–40 | **0–10%** | score 10 → position 0.25 → **2%** |
| REGULAR | 40–70 | **10–20%** | score 55 → position 0.5 → **15%** |
| LOYAL | 70–100 | **20–30%** | score 85 → position 0.5 → **25%** |

The score bands reuse the *same* thresholds as the tier ladder, so retuning a tier threshold
automatically retunes the discount curve inside it — the two can't drift apart.

### Three independent gates, all of which must pass

| Gate | CASUAL | REGULAR | LOYAL | Direction |
|---|---|---|---|---|
| Max order value | ₹2,000 | ₹3,000 | ₹4,000 | scales **up** with tier |
| Max offers per 30 days | 3 | 2 | 1 | deliberately **inverted** |
| Tier eligibility | ✅ | ✅ | ✅ | NEW and RISK never |

**Why the frequency caps are inverted:** LOYAL gets the biggest discount but the fewest shots at
it; CASUAL the smallest discount but the most. If frequency *and* discount *and* cancel-tolerance
all scaled up together, the tier with the most room to exploit them would get all three at once.
The net effect is near-equal discount exposure per customer per 30 days (1 × 30% ≈ 3 × 10%).

**Every value cap sits below the high-value review floor (₹5,000)** — so an incentive can never
auto-apply in the range where a failed payment would already be routed to a human.

The three gates are stored as **three separate booleans** on the `CartEvent` row, so the insights
layer can tell you which gate was actually the blocker rather than parsing text.

### NEW and RISK are hardcoded out, four times over

This is an anti-abuse guardrail, not a tunable economics parameter — it can't be reopened by
editing `.env` or via an LLM suggestion:

1. `config.incentive_eligible_tier_set()` intersects the configured string against
   `{casual, regular, loyal}`.
2. `runtime_flags.set_casual_tier_incentive_eligible()` applies the same intersection on the
   live-mutation path.
3. `rules_engine.py` re-checks tier membership before computing `incentive_ok`.
4. `runtime_flags.get_incentive_pct_band()` returns `None` for them, so the percentage
   calculation yields 0 even if everything else somehow let them through.

### Offers follow the cart

The pinned quantity is the **percentage**, not the amount. `/cart`'s `active_offer` re-prices
against the *current* cart on every render, and `/checkout` applies the identical rule, so what
the cart page shows and what checkout charges can never disagree.

- **Cart edited above the tier cap** → the discount **suspends** (`within_cap: false`, no
  discounted amount). No state is stored about suspension.
- **Cart edited back under the cap** → it **snaps back**, automatically.
- **Attribution and discount are independent.** An over-cap purchase pays full price but *still*
  counts as a recovery — `recovered_from_cart_event_id` is set either way.
- `incentive_final_amount_inr` means **terms actually redeemed** — set at RESUMED time, or
  `None` when suspended. A DECLINED offer keeps its stale proposal-time figure, which is exactly
  why only RESUMED offers ever book `incentive_cost`.

### The repeat-cancel ladder

| Cancel # (within 180 days) | What happens |
|---|---|
| 1st | Normal per-tier cancel branch |
| 2nd | Same — there is no distinct 2nd-cancel rung |
| **3rd+** | `escalate_to_human` (0.88), overriding every tier including LOYAL |

Escalated cancels get no resume card by design, so their at-risk money is closed out by the
maintenance sweep instead.

---

## Money: three at-risk buckets

`revenue.py` is a single-row ledger. Every mutation goes through one `adjust()` function, which
clamps buckets at 0 and writes a `revenue_state_updated` audit entry — so every ledger movement
is reconcilable against the hash chain rather than trusted at face value.

| Field | Meaning |
|---|---|
| `total_revenue` | Confirmed captured, all-time |
| `at_risk_soft` | Silent abandons / nudges — real recovery targets |
| `at_risk_declined` | Explicit cancels — tracked, weighted down |
| `at_risk_failed` | Failed payments — its own thread, its own exits |
| `total_recovered` | At-risk money that later converted |
| `total_lost` | Given up as unrecoverable |
| `incentive_cost` | Money given away via discounts |

Derived on read: `total_at_risk` (the three buckets summed) and
**`net_recovered = total_recovered − incentive_cost`**.

### The single-exit rule

**Three buckets, three threads — never crossed.** Every rupee that enters an at-risk bucket must
leave through exactly one exit: **recovered** (a capture) or **lost** (declined, expired,
superseded, given up, or lapsed by the sweep). A dead-end state that skips both would strand the
money "at risk" forever and starve the recovery rate — which is why the maintenance sweep exists
to close every path that would otherwise have no exit.

**At-risk is always booked at full value.** A discount is a cost paid later to recover the money,
not a reduction in what was at risk — so it never shrinks the at-risk figure, and shows up
separately as `incentive_cost` only once actually redeemed.

### Exactly-once, twice over

Two separate guards, deliberately not merged:

| Flag | Guards | Why separate |
|---|---|---|
| `Order.revenue_recorded` | Capture-side money accounting | Both `/checkout/verify` (browser callback, HMAC-verified, usually first) and the `payment.captured` webhook can confirm the same capture. Claimed atomically by a single conditional `UPDATE … WHERE revenue_recorded = false` — whoever gets the row does *all* the accounting, everyone else no-ops. |
| `Order.risk_settled` | The failed-payment thread | An order that failed and then captured must be able to release its risk *and* book its revenue as two distinct claims. Overloading one flag would break the same-order retry case. |

`order.status` alone must **never** gate money — that was a real bug: verify flipped status
without booking, so the webhook then saw `CAPTURED` and skipped booking too, and storefront
revenue was never booked at all.

### How the failed-payment thread settles

At-risk is booked **once per purchase run**, not per retry — the run's "carrier" is the first
failure, and retries are marked settled immediately with an `at_risk_booking_skipped` audit
entry. It's then released by exactly one of:

1. **Any capture of the same basket** — same-order retry or a brand-new `/checkout` order —
   moves it to `total_recovered` and audits `payment_failure_recovered`.
2. **`POST /checkout/give-up-failed`** — the store's give-up button — settles every open run to
   `total_lost`. It deliberately writes **no cart event**, because that would double-track the
   same money on two threads.
3. **The maintenance sweep's lapse job**, once the nudge window passes.

### Cancel consolidation

An explicit cancel consolidates *all* of the customer's open offers and live attribution signals:
each is released from its own bucket, the new cancel books its own amount, and any **shortfall**
(the summed released amounts minus the new cancel amount) books to `total_lost`. The **sum** is
used rather than the max because when several threads consolidate at once, every released rupee
must either be re-booked or land in `total_lost` — keeping only the largest would drop the rest
from the ledger. Failed-payment money is never touched by any of this.

---

## Audit ledger

Every state change routes through one `write_audit_entry()` call into an append-only table. Rows
are never updated or deleted.

Each entry stores `sequence_num`, `action_type`, `details` (JSON, serialized with sorted keys for
determinism), `prev_hash`, and `entry_hash`, where:

```
entry_hash = sha256( prev_hash | sequence_num | action_type | details | timestamp )
```

The first entry chains from a genesis hash of 64 zeros. **31 distinct action types** are written
across the codebase, covering webhook receipt and rejection, decisions, executed actions, LLM
fallbacks, every cart-event transition, every revenue mutation, tier changes, policy
recommendations and their application, and maintenance sweeps.

Two details worth knowing:

- **`revenue_state_updated` fires on every single ledger mutation**, so the money and the audit
  log can be reconciled against each other.
- **The audit log is a real data source, not write-only.** The recovery analysis reads back
  `payment_failure_given_up` entries, because "customer gave up" and "silently lapsed" are
  indistinguishable from the `Order` row alone — both end `risk_settled = True`.

**What `GET /audit/verify` actually proves:** it walks the whole chain and confirms each entry's
`prev_hash` matches the previous entry's `entry_hash` — so nothing has been deleted, reordered,
or spliced in. That's chain-of-custody. It does **not** currently re-verify each entry's own
content hash, so in-place tampering with a single row's contents wouldn't be caught. Stated
plainly rather than overclaimed; see *Honest limitations*.

---

## The self-tuning insights layer

On top of the per-event pipeline sits a policy loop: three parallel analyses that read real
aggregated data, propose config changes, and let a merchant preview the exact customer-level
impact before applying one.

Each analysis is split the same way — a **pure aggregation module** (no LLM, no writes, no
network) plus an **LLM module**, sharing one router:

| Analysis | Aggregator | What it measures | Whitelisted params |
|---|---|---|---|
| **Incentive economics** | `insights.py` | Redemption rates, incentive lift vs. plain reminders, discount spend, net recovered per tier, and which of the three gates actually blocked offers | 14: per-tier value caps, discount band bounds, 30-day frequency caps, `nudge_expiry_hours`, `casual_tier_incentive_eligible` |
| **Loss & recovery** | `recovery_insights.py` | The whole funnel in one comparable shape — silent abandon, explicit cancel, payment failure — plus failure-reason economics, give-up behaviour, retry effectiveness, escalation analysis, and agent reliability | 3: `confidence_threshold`, `max_auto_retries`, `high_value_amount_inr` |
| **Tier model** | `tier_insights.py` | Tier distribution, per-tier net gain, score distribution, near-miss customers, and whether risk flags are being held too long | 11: score thresholds, attempt floors, score-calibration targets, and the three RISK-gate params |

Two rules keep the loss analysis honest: a cart that was resumed and then failed at the gateway
is reported as **handed to checkout**, not as a cart loss (its money moved to the failed-payment
thread, so counting both would double-count it); and a **give-up is a resolution of a
payment-failure run, not a fourth leak** — its money is already inside that row's "lost."

**Deterministic patterns, separately from the LLM.** Each aggregator also produces a
plain-English `patterns` list computed in Python precisely so the wording is controlled and it
survives the model being unreachable.

**No fallback, by design.** Unlike per-event decisions, if the LLM call fails here,
`recommendations` is empty and `llm_error` is set. A fabricated policy recommendation is worse
than none.

### Applying a recommendation

`POST /insights/apply-suggestion` validates against the **union** of the three whitelists, then:

1. Casts to the right type (bool / float / int) and rejects anything unparseable.
2. Range-checks — `confidence_threshold` to 0–1, tier scores to 0–100, rate params to 0–1,
   attempt floors to ≥ 1.
3. Runs **nine ordering guards** that reject any change inverting a ladder boundary — regular
   score must stay strictly below loyal, each discount band must not reach above the next tier's
   floor, each value cap must not exceed the next tier's, and so on. The guard substitutes the
   proposed value for whichever side is being changed and reads the other live, so the constraint
   holds whichever end you move.
4. Mutates `runtime_flags` and audits `policy_recommendation_applied` with before/after values.

Changes are **in-memory only** — they take effect immediately across all future decisions without
a restart, but do not edit `.env` and do not survive a restart. That's a deliberate scope limit,
and the API says so in its response.

### Tier params get a second safety step

Applying a tier threshold changes how tiers are *computed* but re-evaluates nobody. Moving
customers requires an explicit two-step flow:

- **`GET /insights/tier-reevaluation-preview`** — read-only, writes nothing. Shows exactly which
  customers would move and where, optionally against a hypothetical value (`compute_tier` with
  overrides; `runtime_flags` is never touched).
- **`POST /insights/tier-reevaluation-commit`** — persists the recomputed tiers through the same
  `refresh_tier` hook, so every individual change still writes its own `tier_changed` entry.

---

## API surface

Merchant/dashboard endpoints are open by design (no auth); customer endpoints need
`Authorization: Bearer <token>`.

| Router | Endpoints | Auth |
|---|---|---|
| **Webhook** | `POST /webhooks/razorpay` | HMAC signature |
| **Auth** | `POST /auth/register`, `POST /auth/login` · `GET /auth/me` | open · Bearer |
| **Storefront** | `GET /catalog` · `GET /cart`, `POST /cart/add`, `POST /cart/remove`, `DELETE /cart` (explicit cancel), `POST /cart/resume`, `POST /cart/decline-resume`, `GET /cart/pending-signals` | open · Bearer |
| **Checkout** | `POST /checkout`, `POST /checkout/verify`, `POST /checkout/give-up-failed` | Bearer |
| **Merchant** | `GET /merchant/revenue`, `/merchant/customers`, `/merchant/cart-events`, `/merchant/orders` | open |
| **Outcomes** | `GET /outcomes`, `GET /outcomes/events` | open |
| **Audit** | `GET /audit/log`, `GET /audit/verify` | open |
| **Insights** | `GET /insights/{incentive,recovery,tier}-analysis`, `GET /insights/tier-config`, `POST /insights/apply-suggestion`, `GET /insights/tier-reevaluation-preview`, `POST /insights/tier-reevaluation-commit` | open |
| **Debug** | `POST /debug/toggle-llm-failure`, `POST /debug/simulate-abandonment` · `POST /debug/simulate-cart-timeout` | open · Bearer |

---

## Tech stack

- **Backend**: FastAPI, SQLAlchemy 2.0, PostgreSQL, Pydantic Settings
- **Decision agent**: Groq's free-tier API, called directly over `httpx` with a function-calling
  tool schema — **no vendor SDK anywhere** in this project, for either Groq or Razorpay, and no
  free-text parsing of the model's output
- **Payments**: Razorpay Test Mode — real Orders API, real Standard Checkout, real webhook
  signature verification
- **Auth**: stdlib only — PBKDF2-HMAC-SHA256 (100k iterations, per-user salt) for passwords, and
  a hand-rolled HMAC-signed bearer token. No passlib, no PyJWT
- **Frontend**: React 19, TanStack Router + TanStack Query, Vite, Tailwind 4, shadcn/radix —
  plain client-side SPA, served same-origin by FastAPI, so **there is no CORS middleware
  anywhere**, by design

---

## Setup

Roughly 10 minutes from clone to running, most of it waiting on `npm install`.

### 0. Prerequisites

| Need | Version | Install |
|---|---|---|
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) · macOS: `brew install python@3.12` |
| Node | `^20.19` or `>=22.12` (Vite 8's requirement) | [nodejs.org](https://nodejs.org) · macOS: `brew install node` |
| PostgreSQL | 14+ | macOS: `brew install postgresql@16 && brew services start postgresql@16` · Ubuntu: `sudo apt install postgresql && sudo systemctl start postgresql` · [Windows installer](https://www.postgresql.org/download/windows/) |

Check all three:

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
> time, so if you skip `npm run build`, uvicorn fails with
> `RuntimeError: Directory ... does not exist` before the server even starts. Re-run
> `npm run build` after any frontend change — `uvicorn --reload` does not rebuild the SPA.

### 2. Create the database

```bash
createdb revenue_recovery          # tables auto-create on first server start
```

No migrations to run — `Base.metadata.create_all` builds the schema on startup.

**Your connection string depends on how Postgres was installed**, and this is the single most
common setup failure. Set `DATABASE_URL` in `.env` to match:

| Install | Connection string |
|---|---|
| macOS / Homebrew | `postgresql://<your-mac-username>@localhost:5432/revenue_recovery` (no password) |
| Ubuntu / Debian default | `postgresql://postgres:postgres@localhost:5432/revenue_recovery` |
| Docker `postgres` image | `postgresql://postgres:<your-password>@localhost:5432/revenue_recovery` |

Verify it before starting the server: `psql revenue_recovery -c '\conninfo'`

### 3. Configure `.env`

```bash
cp .env.example .env
```

Then fill in three things:

| Variable | Where to get it | Required? |
|---|---|---|
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | [Razorpay Dashboard](https://dashboard.razorpay.com) → switch to **Test Mode** → Account & Settings → API Keys → Generate. Key id starts `rzp_test_`. | For checkout |
| `RAZORPAY_WEBHOOK_SECRET` | **You invent this** — any string. It only needs to match what you enter in Razorpay's webhook settings (and it's what `simulate_webhook.py` signs with). | For webhooks |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys. Free, no card required. | Optional |
| `DATABASE_URL` | See the table above. | Yes |

**What works if you skip a key** — the app degrades deliberately rather than crashing, so you
can evaluate most of it with no accounts at all:

| Missing | Effect |
|---|---|
| `GROQ_API_KEY` | **Everything still works.** Every decision falls back to the deterministic rules engine, tagged `source: "rules_engine_fallback"` with an `llm_failure_fallback` audit entry. This is the same path the failure-injection demo exercises on purpose. |
| Razorpay keys | Storefront browsing, cart, cart-abandonment recovery, tiering, insights and the audit log all work. Only `POST /checkout` fails (clear 500: *"RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not configured"*). |
| `RAZORPAY_WEBHOOK_SECRET` | Webhooks are rejected rather than silently accepted — a missing secret is treated as a config error, never a bypass. `simulate_webhook.py` also refuses to run. |

### 4. Run it

```bash
# Always run from the project root - main.py resolves frontend/dist relatively.
uvicorn app.main:app --reload
```

Wait for uvicorn's `Application startup complete.` Three background threads
(`dropoff-poller`, `cart-idle-sweep`, `maintenance-sweep`) start silently alongside it — they
only log once they actually do something, so no extra output on boot is expected.

### 5. Confirm it's actually working

```bash
curl localhost:8000/merchant/revenue     # -> the ledger as JSON
curl localhost:8000/audit/verify         # -> {"chain_intact": true, ...}
```

Then open these — all seven are real pages, not placeholders:

| URL | What |
|---|---|
| `http://localhost:8000/` | Landing page |
| `http://localhost:8000/dashboard` | Merchant console (no auth) |
| `http://localhost:8000/store` | Demo storefront (real Razorpay checkout, sign in/register) |
| `http://localhost:8000/architecture` | Live decision-pipeline walkthrough |
| `http://localhost:8000/tiers` | Live customer-tiering walkthrough |
| `http://localhost:8000/login` · `/support` | Customer sign-in · support page |
| `http://localhost:8000/docs` | Interactive API docs (FastAPI/Swagger) |

Now jump to the **Demo walkthrough** below — start by seeding the demo accounts.

### Paying with a test card

Razorpay Test Mode never charges real money. Use card **4111 1111 1111 1111**, any future
expiry, any CVV, any name; on the simulated bank page choose **Success**. Razorpay's full list
of test cards and failure-simulation cards is
[here](https://razorpay.com/docs/payments/payments/test-card-details/).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `RuntimeError: Directory '.../frontend/dist/assets' does not exist` | The SPA wasn't built. Run `cd frontend && npm run build && cd ..` |
| `connection to server ... failed` / `role "postgres" does not exist` | `DATABASE_URL` doesn't match your Postgres install — see the table in step 2. On Homebrew macOS the user is your macOS username with no password. |
| `createdb: command not found` | Postgres isn't on `PATH`. macOS: `brew services start postgresql@16` and re-open the shell. |
| `psycopg2` fails to build during `pip install` | Install Postgres client headers first — Ubuntu: `sudo apt install libpq-dev python3-dev`. The pinned wheel (`psycopg2-binary`) normally avoids this. |
| Dashboard is empty | Expected on a fresh DB — run the seed scripts in the Demo walkthrough. |
| Every decision says `rules_engine_fallback` | No `GROQ_API_KEY`, an invalid one, or the LLM-failure toggle is on. Check with `curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":false}'` |
| `POST /checkout` returns 500 | Razorpay test keys missing from `.env`. |
| Webhook returns 403 | `RAZORPAY_WEBHOOK_SECRET` in `.env` doesn't match the one used to sign the request. |
| Frontend changes don't show up | `uvicorn --reload` doesn't rebuild the SPA. Re-run `npm run build`. |

### Receiving real Razorpay webhooks (optional)

Razorpay only delivers to a public URL, so a local server needs a tunnel. This project uses
**zrok**:

```bash
zrok enable <your-account-token>     # one-time, per machine
zrok share public localhost:8000     # prints a public HTTPS URL
```

Put that HTTPS URL + `/webhooks/razorpay` into Razorpay Dashboard → Settings → Webhooks, select
`payment.failed`, `payment.captured`, `subscription.pending`, `subscription.halted` and
`subscription.charged`, and use the same secret as `RAZORPAY_WEBHOOK_SECRET` in `.env`.

The public URL changes each time you start a share, so re-paste it into Razorpay whenever you
restart the tunnel. Everything in the demo below works **without** a tunnel — the simulator
script signs its own payloads locally.

---

## Demo walkthrough for judges

### 1. Seed the demo accounts and watch each ladder branch fire

```bash
python scripts/seed_storefront_customers.py   # 5 named accounts with backdated history
```

Log in at `/store` with any of these (password `password123` for all) and use the **"Try the
recovery agent"** panel. Each account is engineered to trip a *different* branch:

| Account | State | Do this | Expected decision |
|---|---|---|---|
| `priya@demo.com` | trusted, long history | add an item → **Simulate cart timeout** | `offer_incentive` — tier-sized discount |
| `rahul@demo.com` | new, no history | add an item → **Simulate cart timeout** | `send_reminder`, no incentive (NEW is never eligible) |
| `neha@demo.com` | risk-flagged | add an item → **Delete cart** | `no_action` — not worth chasing |
| `zara@demo.com` | 3 prior cancels | **Delete cart** | `escalate_to_human` — the 4th cancel trips the ladder |

Then check `/dashboard` → Event Feed to see the decision, confidence, and reasoning for each.

### 2. Seed the analysis data

```bash
python scripts/seed_analysis_data.py    # synthetic customers across every analysis's edge cases
```

Both seeders write straight to the DB with backdated history — no server needed.

### 3. Trigger a payment-failure scenario live (server running)

```bash
python scripts/simulate_webhook.py insufficient_funds
python scripts/simulate_webhook.py risk_block      # always escalates, no exceptions
python scripts/simulate_webhook.py high_value      # escalates on amount alone
python scripts/simulate_webhook.py card_expired
python scripts/simulate_webhook.py halted          # subscription.halted
```

The script HMAC-signs its own payload, so this works with no tunnel. Passing the same event id
twice is the documented way to exercise the **idempotency** path.

### 4. Prove the graceful-failure path

Nothing crashes and nothing goes silent when the LLM is unreachable:

```bash
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":true}'
python scripts/simulate_webhook.py insufficient_funds
# response shows source: "rules_engine_fallback" - decision still made and executed
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":false}'
```

Then pull `/audit/log` and point at the `llm_failure_fallback` entry.

### 5. Verify the audit chain

```bash
curl localhost:8000/audit/verify
```

Or Dashboard → Audit Log → **🔐 Verify Chain**.

### 6. Walk the money

`/merchant/revenue` for the ledger snapshot, or `/insights/recovery-analysis?range=all` for the
whole funnel broken out by loss signal with a recovery rate. The dashboard's Overview and
Revenue & Customers tabs show the same numbers, including the three at-risk buckets separately.

### 7. Try a self-tuning suggestion

Dashboard → Audit Log → any of the three analysis modals (📊 Incentive / 📉 Loss & Recovery /
🎯 Tier) → **Analyze** → **Preview Impact** on a recommendation → **Apply**. Every step is
audited. Tier recommendations additionally show you exactly which customers would change tier
before you commit.

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

The design rationale for each module lives in its own docstring and inline comments — the long
comments in this codebase record *why* a rule is shaped the way it is and which bug it guards
against, and are worth reading before changing the logic around them. For the same material as
live diagrams, run the app and open `/architecture` and `/tiers`.

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

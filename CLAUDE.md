# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI + PostgreSQL "revenue recovery agent" built on Razorpay Test Mode. It intercepts
revenue-loss signals (failed payments, checkout drop-offs, pre-checkout cart abandonment),
decides a **bounded** recovery action, gates risky/low-confidence cases, executes, and writes
every step to an append-only hash-chained audit log. It also ships a demo storefront and a
merchant dashboard as static HTML.

There is **no test suite** and no linter config in this repo. Verification is done by running
the server, replaying scenarios through the seed/simulate scripts, and reading `/audit/log`.

## Commands

```bash
source venv/bin/activate
pip install -r requirements.txt
createdb revenue_recovery                       # tables auto-create on startup (Base.metadata.create_all)
# NOTE: no Alembic/migrations - create_all only adds missing TABLES. Adding/renaming a
# column on an existing model needs a manual ALTER (or drop the table) before it appears.

cd frontend && npm install && npm run build && cd ..   # build the SPA once (see Frontend section) - `dist/` must exist

# Always run from the project root - main.py serves frontend/dist/index.html via relative FileResponse paths.
uvicorn app.main:app --reload                   # http://localhost:8000  (/docs, /dashboard, /store, /support)
```

Scripts (all read `.env` via python-dotenv; run from project root):

```bash
python scripts/simulate_webhook.py insufficient_funds|card_expired|risk_block|halted|high_value
python scripts/seed_demo_data.py                        # via HTTP - server must be running
python scripts/seed_storefront_customers.py             # direct DB write - server NOT needed
python scripts/seed_analysis_data.py                     # direct DB - feeds incentive/recovery/tier analyses
./capture_test.sh "<label>"                             # appends DB rows + API snapshots to test_results.txt
```

Live demo levers (no restart needed):

```bash
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":true}'
curl -X POST localhost:8000/debug/simulate-abandonment -H 'Content-Type: application/json' -d '{"abandonment_count_override":2}'
curl localhost:8000/audit/verify
```

## Architecture

### Three signal sources, one decision shape

Every pipeline follows the same sequence: **record event → deterministic rules decision →
LLM enrich (bounded) → gate → `execute_action` → audit at every step**.

| Signal | Entry point | Rules fn | Row written |
|---|---|---|---|
| Payment failure / subscription events | `webhook.py` (Razorpay webhook, HMAC-verified) | `rule_based_decision` | `Event` + `Decision` |
| Checkout drop-off (order created, never paid) | `dropoff.py` (background poll thread, no webhook exists for this) | `rule_based_dropoff_decision` | `Event` + `Decision` |
| Pre-checkout cart event (no Razorpay order exists yet) | `storefront.py::_handle_cart_event` | `rule_based_cart_event_decision` | `CartEvent` (no `Event`/`Decision`) |

`webhook.py` returns `"accepted"` to Razorpay immediately and runs the decision + action in a
FastAPI `BackgroundTask` (`_process_decision_and_action`, which opens its own `SessionLocal`).
So right after posting a webhook the `Decision` row does not exist yet - check
`/outcomes/events` or `/audit/log` a moment later. Two daemon threads start on app startup:
the dropoff poller and the maintenance sweep (`maintenance.py`), which periodically re-refreshes
every customer's tier (the engagement score has time-decaying components that go stale on
dormant accounts) and moves stale `CREATED` orders to `OrderStatus.ABANDONED` — the only code
path that ever sets that status. Aggregations filter to `CAPTURED`/`FAILED`; keep doing that.

The third path is deliberately separate: `Event` only ever represents something Razorpay knows
about. `CartEvent` splits into `silent_abandon` vs `explicit_cancel`, treated as different
*signal strength*, not just different labels.

### Bounding the LLM

`rules_engine.py` is the deterministic baseline and must stay independently readable/testable —
never fold LLM calls into it. `llm_agent.py` (Groq function-calling, 8s timeout) may only
confirm or adjust confidence/reasoning; the action is constrained to the `AgentAction` enum by
tool schema. Any failure — including the `/debug/toggle-llm-failure` switch — returns
`source: "rules_engine_fallback"` and writes an `llm_failure_fallback` audit entry. Nothing
crashes and nothing goes silent.

After the LLM, a **confidence gate** can force `ESCALATE_TO_HUMAN`. `webhook.py` exempts
`retry_now` from the gate (see the comment there — gating it would kill the unknown/invalid_card
path); `dropoff.py` does not. The cart-event path has no LLM layer at all — rules only.

`actions.py` is a stub layer: no real retry/email/SMS call is made. It must never report
`"recovered"`. Real recovery is only ever confirmed by a subsequent `payment.captured` webhook.

### Audit ledger (`audit.py`)

`audit_log` rows are append-only and hash-chained via `prev_hash`. Never update or delete rows,
and route every state change through `write_audit_entry`. Note that `verify_chain` currently
only checks `prev_hash` linkage — it recomputes `expected` but never compares it, so content
tampering within an entry is not actually detected.

### Money and tiers

- `revenue.py` is a single-row ledger (`MerchantRevenueState`). Every mutation goes through
  `adjust()`, which also writes a `revenue_state_updated` audit entry. Buckets clamp at 0.
  At-risk amounts are booked at **full** value; a discount shows up later as `incentive_cost`
  at capture time (`resolve_cart_recovery`), never as a smaller at-risk figure. Every rupee
  entering an at-risk bucket must leave through exactly one of `resolve_cart_recovery` (capture)
  or `resolve_cart_loss` (offer declined/expired/superseded, no-action cancel, or the
  maintenance sweep closing a lapsed card/nudge window) — a dead-end state that skips both
  strands the money "at risk" forever and starves `total_lost`/the recovery rate.
- **Three at-risk buckets, three separate threads — never cross them**: `at_risk_soft`
  (silent abandons/nudges), `at_risk_declined` (explicit cancels), `at_risk_failed` (failed
  payments). Failed-payment runs live entirely on their own thread via `Order.risk_settled`
  (exactly-once guard, separate from `revenue_recorded` on purpose): one `at_risk_failed`
  booking per run (`webhook._open_run_carrier` gates it — settled/seed rows never suppress a
  fresh booking), released to recovered on any capture of the same basket (same-order or
  new-order retry), to lost via `POST /checkout/give-up-failed` (the store's tutorial give-up
  button; deliberately NO cart event — that would double-track) or the sweep's lapse job.
  Cancel consolidation touches CART-thread state only (pending offers + live timeout signals);
  its shortfall uses the **sum** of superseded amounts (≡ the agreed max() rule in the
  single-thread case) so multi-thread consolidation can't drop rupees. Escalated cancels
  (repeat-cancel ladder — no resume card by design) lapse to lost via the sweep.
  **Seed scripts must create Order rows with `revenue_recorded=True, risk_settled=True`** —
  seeded history that looks like open ledger state gets lapsed into phantom losses by the sweep.
- Capture accounting is **exactly-once** via `revenue.record_capture_revenue()` and the
  `Order.revenue_recorded` flag (atomic claim). Both confirmation paths call it —
  `/checkout/verify` (HMAC-verified browser callback, usually first) and the
  `payment.captured` webhook (fallback, and sole handler for payments with no local order).
  Never book capture revenue any other way: `order.status` alone must not gate money (that
  was a real bug — verify flipped status without booking, so the webhook skipped booking too).
- `tiering.py::refresh_tier` is the only place a tier is ever persisted (all call sites go
  through it, which is what makes `tier_changed` auditing complete). `compute_tier`/`tier_breakdown`
  with `overrides=...` is used *only* by the tier-reevaluation preview and must not write anything.

### Tiering: engagement score, not payment success rate

Tiers are `NEW` and `RISK` (states, off the ladder) plus the climbable ladder
`CASUAL → REGULAR → LOYAL`, driven by a 0–100 **engagement score** (`tiering.py`) with hardcoded
weights: completion 30, frequency 25, monetary 20, recency 15, nudge-responsiveness 10. The
module docstring explains why success rate was abandoned — read it before touching this file.
Key invariants:

- **Retries are collapsed into purchase attempts** (`purchase_attempts()`): `/checkout` mints a
  new `Order` row per retry, so FAILED, FAILED, CAPTURED for the same item signature is *one*
  successful attempt, not a 33% success rate. `webhook.py::_resolve_storefront_attempt_count`
  uses the same grouping semantics — keep them aligned.
- Only **customer-attributable** failures count against a customer (`ATTRIBUTABLE_FAILURES` set);
  `NETWORK_ERROR`/`BANK_DECLINE`/`UNKNOWN` are infrastructure and are excluded from the
  completion denominator entirely.
- `RISK` is an enforcement gate checked *before* the score ladder (risk_block flag, attributable
  failure rate, cancel rate) — it is not reachable by merely scoring low.
- Behaviour counts (cancels, abandons) are **windowed** (`tier_behavior_window_days`), never
  all-time — an old fix so customers aren't penalised forever.
- **Incentive % is per-tier bands, not one flat rate**: `incentive_pct_for_customer()` maps the
  customer's position inside their tier's score band onto that tier's discount band
  (casual 0–10, regular 10–20, loyal 20–30 by default). Frequency caps are deliberately
  *inverted* (loyal: biggest discount, fewest per 30d). Fully deterministic — no LLM on this
  path — and the chosen % is snapshotted onto the `CartEvent` row.

### Idempotency

- Inbound webhooks dedupe on `WebhookEventLog.razorpay_event_id`, taken from the
  `X-Razorpay-Event-Id` **header** (not `payload["event"]`, which is only the event *type*),
  falling back to `bodyhash:<sha256>` for local tooling.
- `_sync_storefront_order_failed` only acts when `status == CREATED`, so a duplicate or late
  `payment.failed` can never move a CAPTURED order back to FAILED.
- Repeated silent-abandons of the same cart return the original decision via
  `storefront.py::_find_open_duplicate_abandon` instead of booking the loss twice; an explicit
  cancel of a cart with an open silent-abandon *supersedes* it (releases its `at_risk_soft` and
  consumes its attribution signal) so one cart never sits in both at-risk buckets.

### Runtime flags vs config

`config.py` (`Settings`, from `.env`) holds start-up values; `runtime_flags.py` holds
process-local, in-memory overrides that survive only until restart. **Decision code must read
thresholds through `runtime_flags`, never `settings` directly** — otherwise live changes made
via `/insights/apply-suggestion` and `/debug/*` silently do nothing. (A few call sites still
read `settings` for dropoff-lookback and nudge expiry; that's the existing shape.)

Two guardrails are deliberately *not* tunable: `CustomerTier.NEW` and `RISK` can never become
incentive-eligible (hardcoded out in both `config.incentive_eligible_tier_set()` and
`runtime_flags.set_casual_tier_incentive_eligible()`), and the discount % a customer sees is
always the deterministic band formula — the LLM never chooses money amounts. The engagement-score
component *weights* are hardcoded on purpose (no knob, no runtime flag).

Incentive offers **follow the cart** (no exact-item match): the % rides an edited cart while the
customer's per-tier amount cap (`incentive_max_order_value_{casual,regular,loyal}`, scaling up
with tier, all below the high-value review threshold) keeps holding — over the cap the discount
suspends, and `/cart`'s `active_offer` is the single source of truth the UI and `/checkout` both
price from. Attribution and discount are independent: an over-cap or signal-attributed purchase
still books recovery at full price. `incentive_final_amount_inr` means *terms actually redeemed*
(set/cleared at RESUMED time); `resolve_cart_recovery` books incentive_cost only for RESUMED
offers — a DECLINED offer keeps its proposal-time figure and must never book cost. Declining an
offer with a still-populated cart is NOT a loss (tracked on via a timeout-attribution signal);
an explicit cancel consolidates ALL of the customer's open offers/signals (releases their
at-risk, books any shortfall vs the biggest superseded amount to lost — the max() rule).

### Insights (LLM policy recommendations)

Three parallel analyses, each split the same way: a pure aggregation module (no LLM, no writes)
+ an LLM module + shared router. Each aggregation module also produces a deterministic `patterns`
list (plain-English merchant observations, `{kind, text}`, priority-sorted, capped) rendered by the
shared `PatternsList` frontend component — computed in Python precisely so the wording is
controlled and it survives the LLM being unreachable.

Loss & recovery covers all four loss signals in one comparable shape (silent abandon, explicit
cancel, payment failure, give-up). Two rules keep its money honest: a cart that was resumed and
then failed at the gateway is reported as **handed to checkout**, not as a cart loss, because
`resolve_cart_to_failed_thread` moved that money to the failed-payment thread (counting both would
double-count it); and a **give-up is a resolution of a payment-failure run, not a fourth leak** —
its money is already inside that row's "lost". Every payment-side metric derives from one shared
`_payment_runs()` model (carrier semantics, matching `webhook._open_run_carrier`) so the three
ad-hoc groupings that used to disagree can't drift apart again.

| Analysis | Aggregator | LLM | Whitelisted params |
|---|---|---|---|
| Incentive economics | `insights.py` | `insights_llm.py` | per-tier `incentive_max_order_value_{casual,regular,loyal}`, `nudge_expiry_hours`, `casual_tier_incentive_eligible`, per-tier `incentive_pct_{casual,regular,loyal}_{min,max}` and `incentive_max_per_30d_{casual,regular,loyal}` |
| Loss & recovery (whole funnel) | `recovery_insights.py` | `recovery_insights_llm.py` | `confidence_threshold`, `max_auto_retries`, `high_value_amount_inr` |
| Tier model | `tier_insights.py` | `tier_insights_llm.py` | `tier_loyal_score`, `tier_regular_score`, `tier_min_attempts_for_{loyal,regular}`, `tier_target_orders_per_month`, `tier_target_aov_inr`, `tier_recency_window_days`, `tier_behavior_window_days`, `tier_risk_min_attempts`, `tier_risk_attributable_failure_rate`, `tier_risk_cancel_rate` |

`insights_router.py` wires all three, plus `/insights/apply-suggestion`, which validates the
param against the union of the three whitelists, casts/range-checks it, mutates `runtime_flags`,
and audits it. `_ORDERED_PAIRS`/`_check_ordering` reject any change that would invert a ladder
boundary (regular score ≥ loyal score, a lower tier's discount band reaching above the next
tier's floor, etc.) — extend that list when adding paired params. Unlike per-event decisions
there is **no fallback** here — if the LLM call fails, `recommendations` is empty and
`llm_error` is set; never fabricate a recommendation.

Tier params are special-cased: applying one changes how tiers are *computed* but re-evaluates
nobody. The merchant must go through `/insights/tier-reevaluation-preview` (read-only diff,
optionally against a hypothetical value) and then `/insights/tier-reevaluation-commit`.

### Frontend

`frontend/` is a React 19 + TanStack Router + Vite SPA (Tailwind 4, shadcn/radix components,
TanStack Query) — plain client-side rendering, no SSR/server functions (TanStack Start's SSR
layer was deliberately stripped; see `frontend/vite.config.ts`). Five routes: `/` (landing),
`/dashboard` (merchant console — Overview/Event Feed/Revenue & Customers/Audit Log tabs, no
auth, every `/merchant/*`, `/outcomes/*`, `/audit/*`, `/insights/*` endpoint is open), `/store`
(customer storefront with real Razorpay Standard Checkout, Bearer-token auth), `/login`
(customer sign-in/register), `/support`.

**Dev**: `cd frontend && npm run dev` (Vite on :5173) — `vite.config.ts`'s `server.proxy`
forwards every backend path prefix (`/auth`, `/catalog`, `/cart`, `/checkout`, `/merchant`,
`/outcomes`, `/audit`, `/insights`, `/debug`) to `localhost:8000`, so all frontend `fetch()` calls
use relative paths and nothing is ever cross-origin — there is no CORS middleware anywhere in
this backend, intentionally.
**Ship**: `npm run build` writes `frontend/dist/`, which `app/main.py` serves directly
(`FileResponse`/`StaticFiles` on `FRONTEND_DIST`) — same origin as the API. Rebuild after any
frontend change; `uvicorn --reload` does not rebuild the SPA for you.

The customer-facing auth token is a Bearer token (`Authorization: Bearer <token>`, not a cookie)
stored client-side in `localStorage` (`frontend/src/lib/auth-token.ts`) — merchant-dashboard
endpoints need no auth at all. API client modules live in `frontend/src/lib/api/` (one file per
backend router); domain enum→label/color maps (actions, outcomes, tiers, cart events, the
~29-entry tunable-param labels) live in `frontend/src/lib/constants/`, ported verbatim from the
original hand-rolled dashboard so badge/label meaning doesn't silently drift. `AuditLogEntry.details`
comes over the wire as a JSON-encoded **string**, not a parsed object — parse defensively (see
`AuditLogTable.tsx`'s `parseDetails`), the same way the original dashboard did.

## Conventions

- Amounts: Razorpay speaks paise, this app stores INR floats. Convert at the boundary
  (`/100.0` in, `int(round(x*100))` out) and use `revenue.round_to_paise_safe` before sending.
- The long comments in this codebase carry design rationale and the history of bugs already
  fixed (attempt-count reconstruction, the three separate incentive gates, event-id dedup).
  Read them before "simplifying" the logic they guard, and keep new rationale in the same style.
- `.env` is real and gitignored-by-convention (no git repo here). Never commit it.

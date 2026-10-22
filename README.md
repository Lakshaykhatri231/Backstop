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
5. [Rules, tiering, incentives, money, audit](#rules-tiering-incentives-money-audit)
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

## Rules, tiering, incentives, money, audit

Thresholds are read live through `runtime_flags`, so a change applied from the analytics layer takes effect without a restart. Auto-execute
needs confidence ≥ **0.70**.

### Decision rules (deterministic, first match wins)

**Payment failure** (`rule_based_decision`)

| Condition | Action | Why |
|---|---|---|
| attempts > 3 | `escalate_to_human` (0.95) | Retry cap is checked first, above even `risk_block` |
| amount ≥ ₹5,000 | `escalate_to_human` (0.60) | Confidence deliberately below the bar, so a person always sees it |
| `risk_block` | `escalate_to_human` (0.85) | Unsafe at any attempt count |
| `card_expired`, `cancelled` | `send_nudge` | A retry cannot succeed / would burn goodwill |
| `unknown`, `invalid_card` | `retry_now` once, then escalate | Escalates at attempt 2 |
| `insufficient_funds` | `retry_later` | The money isn't there yet, so never `retry_now` |
| `bank_decline`, `network_error`, `authentication_failed` | `retry_now`, then `retry_later` | Infrastructure-side failures |
| unrecognised reason | `escalate_to_human` (0.30) | |

**Checkout drop-off** (`rule_based_dropoff_decision`, abandonments counted over 7 days): never-opened → `no_action`; high value → escalate;
1st → `send_reminder`; 2nd → `offer_incentive` if the gates pass, else `send_resume_link`; 3rd+ → escalate.

**Pre-checkout cart event** (rules only, no LLM): the action depends on tier and signal. An explicit cancel is scored lower than a silent
abandon at the same tier, because a silent abandon is ambiguous while a cancel is the customer saying no. A 3rd explicit cancel inside 180
days escalates regardless of tier.

| Tier | Explicit cancel | Silent abandon |
|---|---|---|
| RISK / NEW | `no_action` | `send_reminder` |
| CASUAL / REGULAR / LOYAL | `offer_incentive` if gates pass, else `send_resume_link` | `offer_incentive` if gates pass, else `send_reminder` |

The LLM can only return one of nine `AgentAction` values; the scheduler-only actions are excluded from its tool schema.

### Customer tiering

Tiers decide how much recovery effort a customer is worth. They are driven by an **engagement score**, not payment success rate, which
punished the customers who retried hardest (`FAILED, FAILED, CAPTURED` scored 33%).

- **Order of evaluation:** `NEW` (no attempts) → `RISK` (an enforcement gate, checked before the score) → `CASUAL` → `REGULAR` (score ≥ 40 and
  ≥ 3 attempts) → `LOYAL` (score ≥ 70 and ≥ 5 attempts). Score and volume are AND-ed, so one lucky order can't reach the top.
- **RISK trips on any of:** a prior `risk_block` (never expires), attributable failure rate > 0.60, or cancel rate > 0.60 (both need ≥ 2 attempts).
- **Score (0–100):** `30·completion + 25·frequency + 20·monetary + 15·recency + 10·responsiveness`. Each part is a clamped linear ratio; the
  weights are hardcoded on purpose. Responsiveness defaults to a neutral 0.5 so never needing recovery isn't punished.
- **Retries collapse into one purchase attempt** (grouped by an order-independent `sku:qty` signature; a capture closes the run).
- **Only customer-attributable failures count** (`insufficient_funds`, `card_expired`, `invalid_card`, `authentication_failed`, `risk_block`,
  `cancelled`). `network_error`, `bank_decline` and `unknown` are excluded from the denominator, so infrastructure cannot lower a score.
- **Behaviour is windowed** (cancels, abandons, nudge outcomes: 180 days) so a customer isn't penalised forever. `tiering.py::refresh_tier` is the
  only place a tier is persisted, which keeps `tier_changed` auditing complete.

### Incentives

The discount a customer sees is a formula; the LLM is never on this path. The percentage is the customer's position inside their tier's score
band, mapped onto that tier's discount band: CASUAL 0–10%, REGULAR 10–20%, LOYAL 20–30% (e.g. score 55 → REGULAR, halfway → 15%).

Three gates must all pass: a per-tier **order-value cap** (₹2,000 / ₹3,000 / ₹4,000, all below the ₹5,000 review floor), a per-tier **30-day
frequency cap** (3 / 2 / 1, deliberately inverted so the biggest discount gets the fewest offers), and **tier eligibility**. NEW and RISK are
excluded in four independent places, as an anti-abuse guardrail rather than a tunable.

Offers follow the cart: the percentage is pinned, the amount is re-priced on every read. An edit above the cap suspends the discount (and
restores it once back under); an over-cap purchase still counts as a recovery at full price. `incentive_cost` is booked only for redeemed offers.

### Money

`revenue.py` is a single-row ledger; every change goes through `adjust()`, which clamps at 0 and audits. Failed payments, soft abandons and
explicit cancels each have their **own at-risk bucket and thread, never crossed**. Every rupee that enters an at-risk bucket leaves through
exactly one exit: **recovered** (a capture) or **lost** (declined, expired, superseded, given up, or lapsed by the maintenance sweep). At-risk is
always booked at full value; a discount appears later as `incentive_cost`. `net_recovered = total_recovered − incentive_cost`.

Capture accounting is exactly-once. `/checkout/verify` and the `payment.captured` webhook can both confirm the same payment, so each claims it
with an atomic `UPDATE … WHERE revenue_recorded = false`; the winner does all the accounting. `Order.risk_settled` is a separate claim for the
failed-payment thread, so a retry that captures releases its risk and books its revenue as two distinct events. `order.status` alone never gates
money (a past bug did exactly that).

### Audit ledger

Every state change goes through `write_audit_entry()` into an append-only, hash-chained table (118 action types): `entry_hash = sha256(prev_hash |
sequence_num | action_type | details | timestamp)`, chaining from a genesis of 64 zeros. `revenue_state_updated` fires on every ledger change, so
money and log reconcile. `GET /audit/verify` walks the chain and proves nothing was deleted, reordered or spliced in. It does **not** recompute
each entry's own hash, so editing one row in place would go undetected (see [Limitations](#limitations-and-roadmap)).

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

> The frontend build is not optional: FastAPI serves `frontend/dist`. Re-run `npm run build` after any frontend change; `uvicorn --reload` doesn't. Tables are created on startup.

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

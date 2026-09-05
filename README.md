# Backstop

**Razorpay Buildthon — Track 03: AI Revenue Recovery**

> *Find revenue that's slipping away and win it back.*

Backstop watches for revenue about to leak — a payment that fails, a checkout
someone opens and never finishes, a cart someone walks away from before
checkout even starts — and runs each one through the same bounded pipeline: a
deterministic rules engine decides first, a language model may only refine
that decision within fixed limits, risky or low-confidence calls are handed
to a human, and every step is written to an append-only, hash-chained audit
log. It ships with a real Razorpay Test Mode storefront and a merchant
dashboard, so the whole loop — detect, decide, act, recover — is something a
judge can actually click through, not just read about.

Live, interactive documentation of everything below is built into the app
itself: run it and visit `/architecture` (decision pipeline, money flow) and
`/tiers` (customer tiering formula, benefits) — both pull their numbers live
from the running system, not a static diagram.

---

## How this meets the track's bar

> *"Don't just identify the problem. Show measured money recovered across a
> batch, with compliant escalation, stopping rules, and an audit trail."*

| Requirement | How Backstop does it | Where to see it |
|---|---|---|
| **Measured money recovered, across a batch** | A single-row revenue ledger (`total_revenue`, `total_recovered`, `total_lost`, `incentive_cost`, and three separate at-risk buckets) updated on every state change via one `adjust()` function. Every rupee that enters an at-risk bucket leaves through exactly one exit — capture or loss — so nothing double-counts or gets stranded. | `GET /merchant/revenue`, `GET /insights/recovery-analysis?range=30d`, Dashboard → Overview / Revenue & Customers |
| **Compliant escalation** | A confidence gate forces `escalate_to_human` below a configurable threshold; a high-value floor and a permanent `risk_block` flag escalate unconditionally, no exceptions. The LLM is schema-constrained (Groq function-calling) to a fixed action enum — it can refine confidence and reasoning, but it cannot invent an action or pick a money amount. | Dashboard → Overview → Human Gate Queue |
| **Stopping rules** | Max auto-retries before forced escalation; per-tier discount bands, order-value caps, and 30-day frequency caps (deliberately *inverted* — the tier with the biggest discount gets the fewest shots at it); NEW and RISK customers are hardcoded out of incentive eligibility regardless of config; a 3rd+ cancel skips the offer entirely instead of escalating the discount. | `app/rules_engine.py`, `app/config.py`, `/tiers` |
| **Audit trail** | Every state change — decision, action, revenue mutation, tier change, config change — routes through one `write_audit_entry()` call into an append-only `audit_log` table, hash-chained via `prev_hash`. | `GET /audit/log`, `GET /audit/verify`, Dashboard → Audit Log |

---

## What's actually built

The track lists several example directions; here's what Backstop covers and
how, stated plainly rather than oversold:

| Track direction | Status | Notes |
|---|---|---|
| Payment degradation → root cause → recovery action | **Built** | Razorpay webhook (`payment.failed`, `subscription.*`), HMAC-verified, normalized into an `Event`, decided by `rules_engine.py` against `FailureReason` (insufficient funds, card expired, bank decline, risk block, network error, auth failed, invalid card…) and attempt count. |
| Checkout drop-off recovery | **Built** | No Razorpay webhook exists for "order created, never paid" — Backstop runs its own background poller that detects stale `CREATED` orders and decides on reminders, resume links, or a tier-sized incentive. |
| *(not in the track's list, but the same problem one step earlier)* Pre-checkout cart abandonment | **Built** | A cart that never became a Razorpay order at all — silent abandonment vs. an explicit cancel are treated as genuinely different signal strength, not just different labels, each with its own recovery ladder. |
| Failed-subscription recovery | **Partial** | `subscription.pending` / `subscription.halted` / `subscription.charged` webhooks are ingested and decided by the same rules ladder — there's no subscription-specific win-back playbook beyond that yet. |
| B2B receivables chaser, mandate retry sequencer, Hinglish voice recovery, promise-to-pay tracker | **Not built** | Out of scope for this build; the architecture (bounded rules → LLM refine → gate → audit) is designed to extend to a fourth signal source the same way the third one was added, but none of these four exist yet. |

---

## Architecture

Three signal sources feed into one decision shape:

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

Every rupee that becomes "at risk" is booked at full value into one of three
independent buckets (silent abandons, explicit cancels, failed payments) and
leaves through exactly one exit — recovered or lost — tracked via
exactly-once guards so the same rupee can never be counted twice or dropped
on the floor. A discount never shrinks the at-risk figure; it shows up
separately as `incentive_cost` only once actually redeemed.

On top of the decision pipeline sits a **self-tuning insight loop**: three
parallel LLM-backed analyses (incentive economics, loss & recovery across the
whole funnel, and the tiering model itself) that read real aggregated data,
propose config changes, and let a merchant preview the exact customer-level
impact before applying one — with every recommendation and every applied
change written to the same audit log.

Customer tiering (`NEW` / `CASUAL` → `REGULAR` → `LOYAL` / `RISK`) drives how
much recovery effort a customer is worth. It's a 0–100 engagement score built
from behaviour the customer actually controls — not raw payment success
rate, which used to punish the customers who retried hardest. `RISK` is an
enforcement gate checked *before* the score ladder, not a rung you can fall
into by scoring low. Full breakdown, live thresholds included: `/tiers`.

## Tech stack

- **Backend**: FastAPI, SQLAlchemy 2.0, PostgreSQL, Pydantic Settings
- **Decision agent**: Groq's free-tier API (Llama), called directly over
  `httpx` with a function-calling tool schema — no SDK dependency, no
  free-text parsing of the model's output
- **Payments**: Razorpay Test Mode — real Orders API, real Standard
  Checkout, real webhook signature verification
- **Frontend**: React 19, TanStack Router + TanStack Query, Vite, Tailwind 4,
  shadcn/radix components — plain client-side SPA, same-origin as the API
  (no CORS anywhere, by design)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # venv\Scripts\activate on Windows
pip install -r requirements.txt

createdb revenue_recovery          # tables auto-create on server startup

cp .env.example .env
# Fill in: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (Dashboard -> Test Mode ->
# API Keys), RAZORPAY_WEBHOOK_SECRET (you choose this when creating the
# webhook), GROQ_API_KEY (free, no card — console.groq.com), DATABASE_URL
# if your local Postgres user/password differ.

cd frontend && npm install && npm run build && cd ..   # builds frontend/dist/

# Always run from the project root - main.py serves the built frontend
# via relative paths.
uvicorn app.main:app --reload
```

Then open:
- `http://localhost:8000/` — landing page
- `http://localhost:8000/dashboard` — merchant console (no auth)
- `http://localhost:8000/store` — demo storefront (real Razorpay checkout, sign in/register)
- `http://localhost:8000/architecture` — live decision-pipeline walkthrough
- `http://localhost:8000/tiers` — live customer-tiering walkthrough
- `http://localhost:8000/docs` — interactive API docs

**Exposing it for real Razorpay webhooks** (optional): `ngrok http 8000`,
then put the ngrok HTTPS URL + `/webhooks/razorpay` into Razorpay Dashboard →
Settings → Webhooks, select `payment.failed`, `subscription.pending`,
`subscription.halted`, `subscription.charged`, `payment.captured`, using the
same secret as `.env`.

## Demo walkthrough for judges

**1. Seed realistic data** (no server needed for these two — they write
straight to the DB with backdated history):
```bash
python scripts/seed_storefront_customers.py   # 5 named demo accounts, stable tiers
python scripts/seed_analysis_data.py          # 174 synthetic customers across
                                                # every dashboard analysis's edge cases
```

**2. Trigger a single scenario live**, with the server running:
```bash
python scripts/simulate_webhook.py insufficient_funds
python scripts/simulate_webhook.py risk_block     # always escalates, no exceptions
python scripts/simulate_webhook.py high_value      # escalates on amount alone
```
Then watch it land: `/dashboard` → Event Feed, or `curl localhost:8000/outcomes/events`.

**3. Prove the graceful-failure path** (nothing crashes, nothing goes silent
when the LLM is unreachable):
```bash
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":true}'
python scripts/simulate_webhook.py insufficient_funds
# response shows source: "rules_engine_fallback" - decision still gets made and executed
curl -X POST localhost:8000/debug/toggle-llm-failure -H 'Content-Type: application/json' -d '{"forced":false}'
```
Pull `/audit/log` and point at the `llm_failure_fallback` entry.

**4. Verify the audit chain hasn't been tampered with**:
```bash
curl localhost:8000/audit/verify
```

**5. Walk the money**: `/merchant/revenue` for the ledger snapshot,
`/insights/recovery-analysis?range=all` for the whole funnel broken out by
loss signal (silent abandon, explicit cancel, payment failure, give-up) with
a recovery rate — or just open the dashboard's Overview and Revenue &
Customers tabs.

**6. Try a self-tuning suggestion**: Dashboard → Audit Log → any of the three
analysis modals (📊 Incentive / 🚨 Loss & Recovery / 🎯 Tier) → Analyze →
Preview Impact on a recommendation → Apply. Every step is audited.

## Project structure

```
app/                    FastAPI backend
  webhook.py               Razorpay webhook -> Event/Decision (payment failures, subscriptions)
  dropoff.py                background poller -> Event/Decision (checkout drop-off)
  storefront.py             cart/checkout/auth, cart-event decisions (pre-checkout abandonment)
  rules_engine.py            deterministic baseline, all three signal sources
  llm_agent.py                bounded Groq refinement layer
  revenue.py                   single-row money ledger, exactly-once booking
  tiering.py                    engagement-score customer tiers
  audit.py                       append-only, hash-chained audit log
  insights*.py, *_llm.py, insights_router.py   the self-tuning policy-suggestion loop
  maintenance.py                 periodic tier refresh + stale-order sweep
frontend/               React SPA (dashboard, storefront, architecture & tiers walkthroughs)
scripts/                seeders + a live-webhook simulator (see script docstrings)
CLAUDE.md                deep technical documentation of every design decision and past bug fixed
```

## Honest limitations

There's no automated test suite — verification is by running the server,
replaying scenarios through the seed/simulate scripts, and reading
`/audit/log` / `/audit/verify`. `actions.py` is a stub layer: no real
retry/email/SMS call is ever made, and it never reports "recovered" on its
own — real recovery is only ever confirmed by a subsequent Razorpay
`payment.captured` webhook. `audit.verify_chain` currently checks
hash-linkage between entries but doesn't yet re-verify each entry's own
content hash, so within-entry tampering wouldn't be caught (chain-of-custody
is enforced; content integrity per-entry isn't, yet).

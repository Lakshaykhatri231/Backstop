import { Link } from "@tanstack/react-router";
import { FileText, Link2 } from "lucide-react";

import { ColumnHeader } from "./components/flowchart";
import {
  JourneyColumnCancelled,
  JourneyColumnPayment,
  JourneyColumnTimeout,
  JourneyTrunk,
} from "./components/journey-diagram";
import {
  MoneyLaneExplicitCancel,
  MoneyLanePaymentFailure,
  MoneyLaneSilentAbandon,
} from "./components/money-diagram";
import {
  PipelineColumnStorefront,
  PipelineSharedFlow,
  PipelineTrunk,
} from "./components/pipeline-diagram";
import { SectionHeading } from "./components/primitives";

const SECTIONS = [
  { id: "journey", label: "Customer journey" },
  { id: "pipeline", label: "Decision pipeline" },
  { id: "money", label: "Where the money goes" },
];

export function ArchitecturePage() {
  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <header className="border-b border-ink/10 bg-cream/80">
        <div className="mx-auto max-w-[1320px] px-6 h-16 flex items-center gap-8">
          <Link to="/" className="flex items-center gap-2.5">
            <span className="font-display font-semibold text-lg tracking-tight">Backstop</span>
          </Link>
          <nav className="hidden md:flex items-center gap-1 text-sm">
            <Link to="/dashboard" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Dashboard
            </Link>
            <Link to="/store" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Storefront
            </Link>
            <span className="px-3 py-1.5 rounded-md text-ink font-semibold">Architecture</span>
            <Link to="/tiers" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Tiers
            </Link>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden sm:flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full bg-soft/10 text-soft">
              <span className="size-1.5 rounded-full bg-soft" /> Razorpay Test Mode
            </span>
          </div>
        </div>
      </header>

      <section className="relative overflow-hidden border-b border-ink/10">
        <div className="absolute inset-0 -z-10 bg-gradient-to-br from-amber-100 via-orange-50 to-cream" />
        <div className="mx-auto max-w-[1200px] px-6 py-14">
          <p className="text-xs uppercase tracking-[0.2em] text-ember font-semibold mb-3">
            How Backstop works
          </p>
          <h1 className="font-display text-4xl sm:text-5xl font-semibold tracking-tight leading-[1.05] max-w-3xl">
            One decision shape, three signal sources, every step on the record.
          </h1>
          <p className="text-ink/70 mt-4 max-w-2xl text-[15px] leading-relaxed">
            Backstop watches for revenue about to leak — a failed payment, a checkout someone opened
            and never finished, a cart someone walked away from before checkout even started — and
            runs each one through the same bounded pipeline: a deterministic rules engine decides
            first, a language model may only refine that decision within fixed limits, risky or
            low-confidence calls are handed to a human, and every step is written to an append-only,
            hash-chained log. This page walks through that pipeline end to end.
          </p>
        </div>
      </section>

      <nav className="sticky top-0 z-10 bg-cream/90 backdrop-blur border-b border-ink/10">
        <div className="mx-auto max-w-[1200px] px-6 h-12 flex items-center gap-1 overflow-x-auto text-sm">
          {SECTIONS.map((s) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              className="px-3 py-1.5 rounded-md text-ink/55 hover:text-ink hover:bg-ink/5 whitespace-nowrap shrink-0"
            >
              {s.label}
            </a>
          ))}
        </div>
      </nav>

      <main className="mx-auto max-w-[1200px] px-6 py-16 space-y-24">
        <JourneySection />
        <PipelineSection />
        <MoneySection />
      </main>

      <footer className="border-t border-ink/10 py-8">
        <div className="mx-auto max-w-[1200px] px-6 text-xs text-ink/40">
          Every box on this page names the real module or endpoint behind it — cross-reference
          against the code, not the other way round.
        </div>
      </footer>
    </div>
  );
}

function JourneySection() {
  return (
    <section id="journey" className="scroll-mt-24">
      <SectionHeading
        eyebrow="01 — Customer journey"
        title="What the customer actually experiences"
        description="Same three threads as the sections below, told as a story instead of a system diagram — follow one customer from sign-in to an outcome. Every box, diamond and arrow below is drawn from exact coordinates, not approximated."
      />

      {/* Breaks out of the page's 1200px column so the three branch diagrams get enough physical
          width to render at close to the trunk's scale — squeezed into the normal column width,
          the same shapes would render visibly smaller than the trunk above them. */}
      <div className="relative left-1/2 w-screen -translate-x-1/2">
        <div className="max-w-[1760px] mx-auto px-6">
          <JourneyTrunk />

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-start -mt-2">
            <div>
              <ColumnHeader tone="soft" index={1} label="Timeout" />
              <JourneyColumnTimeout />
            </div>
            <div>
              <ColumnHeader tone="declined" index={2} label="Cancelled" />
              <JourneyColumnCancelled />
            </div>
            <div>
              <ColumnHeader tone="failed" index={3} label="Payment initiated" />
              <JourneyColumnPayment />
            </div>
          </div>
        </div>
      </div>

      <div className="grid sm:grid-cols-2 gap-3 mt-6 text-[11px] text-ink/45 leading-relaxed">
        <p className="rounded-xl border border-dashed border-ink/15 p-3">
          <strong className="text-ink/60">
            Cancel is stricter than timeout, but not on price.
          </strong>{" "}
          The discount %, cap and frequency limit are the same formula either way. What's actually
          tighter: a 3rd+ cancel skips the offer entirely (escalate_to_human, no resume card by
          design), NEW/RISK-tier cancels get nothing at all while a NEW/RISK timeout still gets a
          plain reminder, and every cancel decision carries lower confidence than the equivalent
          abandon decision.
        </p>
        <p className="rounded-xl border border-dashed border-ink/15 p-3">
          <strong className="text-ink/60">"Give up" is not a cart cancel.</strong> It's a separate
          terminal on the failed-payment thread — <code className="font-mono">Order.status</code>{" "}
          stays <code className="font-mono">FAILED</code> forever (there is no{" "}
          <code className="font-mono">CANCELLED</code> status); only{" "}
          <code className="font-mono">risk_settled</code> flips. Deliberately kept separate from the
          Cancelled thread's <code className="font-mono">CartEvent</code> so the two never
          double-track the same loss.
        </p>
      </div>
    </section>
  );
}

function PipelineSection() {
  return (
    <section id="pipeline" className="scroll-mt-24">
      <SectionHeading
        eyebrow="02 — Decision pipeline"
        title="Three signals, one decision shape"
        description="Same flowchart precision as the customer journey above — every box, diamond and arrow drawn from exact coordinates. Payment webhooks and checkout drop-offs share the exact same rules → LLM → gate → execute machinery, so it's drawn once below and both sources feed into it — their two real differences are called out right where they apply. The cart-event column is genuinely shorter and separate — no Razorpay order exists yet, so there's no LLM layer and no gate, rules only."
      />

      <div className="relative left-1/2 w-screen -translate-x-1/2">
        <div className="max-w-[1760px] mx-auto px-6">
          <PipelineTrunk />

          <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-8 items-start -mt-2">
            <PipelineSharedFlow />
            <div>
              <ColumnHeader tone="soft" index={3} label="Storefront cart event" />
              <PipelineColumnStorefront />
            </div>
          </div>
        </div>
      </div>

      <div className="grid sm:grid-cols-2 gap-3 mt-6 text-[11px] text-ink/45 leading-relaxed">
        <p className="rounded-xl border border-dashed border-ink/15 p-3">
          <strong className="text-ink/60">"LLM vs rules' pick?" isn't a real code branch.</strong>{" "}
          <code className="font-mono">llm_agent.py::_run_llm</code> never compares the LLM's answer
          to the rules engine's suggestion — on success it returns whatever the LLM said,
          unconditionally. The diamond describes the range of outcomes the prompt's contract makes
          possible (confirm, adjust confidence/reasoning, or — technically legal but essentially
          never seen, since the prompt never asks for it — a different action), not something the
          code itself checks.
        </p>
        <p className="rounded-xl border border-dashed border-ink/15 p-3">
          <strong className="text-ink/60">Escalation is executed too.</strong> Falling below the
          confidence threshold doesn't skip execution — it just swaps the action to{" "}
          <code className="font-mono">escalate_to_human</code> first. Both branches converge on the
          exact same <code className="font-mono">execute_action()</code> call and the same two audit
          entries (<code className="font-mono">decision_made</code>, then{" "}
          <code className="font-mono">action_executed</code>).
        </p>
      </div>

      <div className="mt-6">
        <AuditChainStrip note="Every 'Audited' box above writes into this same append-only, hash-chained ledger — decision_made / action_executed for the webhook and dropoff threads, cart_event_detected / cart_event_action_executed for the storefront thread." />
      </div>
    </section>
  );
}

function AuditChainStrip({ note }: { note?: string }) {
  const blocks = new Array(7).fill(0);
  return (
    <div className="rounded-2xl bg-ink text-cream p-5">
      <div className="flex items-center gap-2 mb-3">
        <FileText className="size-4 text-tangerine" />
        <p className="font-display font-semibold text-sm">
          Audit log — written at every step above
        </p>
      </div>
      <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
        {blocks.map((_, i) => (
          <div key={i} className="flex items-center gap-1.5 shrink-0">
            <div className="size-8 rounded-md bg-cream/10 border border-cream/15 grid place-items-center">
              <Link2 className="size-3.5 text-cream/50" />
            </div>
            {i < blocks.length - 1 && <div className="w-3 h-px bg-cream/20" />}
          </div>
        ))}
      </div>
      <p className="text-[11px] text-cream/50 mt-3 leading-relaxed max-w-2xl">
        {note ??
          "Append-only and hash-chained via prev_hash. Rows are never updated or deleted — every " +
            "state change routes through write_audit_entry(). Inspect anytime: GET /audit/log · GET /audit/verify."}
      </p>
    </div>
  );
}

function MoneySection() {
  return (
    <section id="money" className="scroll-mt-24">
      <SectionHeading
        eyebrow="03 — Where the money goes"
        title="Three at-risk buckets, three separate threads — never crossed"
        description="Same flowchart precision as the sections above. revenue.py is a single-row ledger — every mutation goes through adjust(), and every rupee that enters an at-risk bucket leaves through exactly one of two exits, booked at full value regardless of any discount offered along the way."
      />

      <div className="relative left-1/2 w-screen -translate-x-1/2">
        <div className="max-w-[1760px] mx-auto px-6">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-start">
            <div>
              <ColumnHeader tone="soft" index={1} label="Silent abandon" />
              <MoneyLaneSilentAbandon />
            </div>
            <div>
              <ColumnHeader tone="declined" index={2} label="Explicit cancel" />
              <MoneyLaneExplicitCancel />
            </div>
            <div>
              <ColumnHeader tone="failed" index={3} label="Payment failure" />
              <MoneyLanePaymentFailure />
            </div>
          </div>
        </div>
      </div>

      <div className="grid sm:grid-cols-2 gap-3 mt-6 text-[11px] text-ink/45 leading-relaxed">
        <p className="rounded-xl border border-dashed border-ink/15 p-3">
          <strong className="text-ink/60">A discount never shrinks the at-risk figure.</strong>{" "}
          Every bucket books at full value the moment money looks at risk. A redeemed incentive
          shows up separately as <code className="font-mono">incentive_cost</code> at capture time (
          <code className="font-mono">resolve_cart_recovery</code>) — the at-risk number itself
          never gets smaller.
        </p>
        <p className="rounded-xl border border-dashed border-ink/15 p-3">
          <strong className="text-ink/60">Cross-thread moves still balance to zero.</strong> A cart
          resumed and then failed at the gateway hands its money to the failed-payment thread
          instead of double-counting it as a cart loss (
          <code className="font-mono">resolve_cart_to_failed_thread</code>). Two independent flags —{" "}
          <code className="font-mono">revenue_recorded</code> for captures,{" "}
          <code className="font-mono">risk_settled</code> for the failed thread — keep every booking
          exactly-once, so the same rupee is never counted twice.
        </p>
      </div>
    </section>
  );
}

import { Link } from "@tanstack/react-router";
import { AlertCircle } from "lucide-react";

import { SectionHeading } from "@/features/architecture/components/primitives";
import { useTierConfig } from "@/lib/hooks/useDashboardData";

import { ScoreFormula } from "./components/ScoreFormula";
import { TierBenefitsGrid } from "./components/TierBenefitsGrid";
import { TierLadder } from "./components/TierLadder";

const SECTIONS = [
  { id: "tiers", label: "The five tiers" },
  { id: "formula", label: "The score formula" },
  { id: "benefits", label: "Benefits & incentives" },
];

export function TiersPage() {
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
            <Link to="/architecture" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Architecture
            </Link>
            <span className="px-3 py-1.5 rounded-md text-ink font-semibold">Tiers</span>
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
          <p className="text-xs uppercase tracking-[0.2em] text-ember font-semibold mb-3">How Backstop tiers customers</p>
          <h1 className="font-display text-4xl sm:text-5xl font-semibold tracking-tight leading-[1.05] max-w-3xl">
            Five tiers, one engagement score, zero guesswork.
          </h1>
          <p className="text-ink/70 mt-4 max-w-2xl text-[15px] leading-relaxed">
            Every customer is either brand New, one of three climbable rungs — Casual, Regular, Loyal — or flagged
            Risk. A single 0–100 engagement score decides the rung, recomputed after every order, every cart event,
            and a periodic sweep so it never goes stale on a dormant account. This page walks through the formula,
            how the tier is actually decided, and exactly what each tier is worth — with the numbers pulled live
            from this merchant's current thresholds, since most of them are tunable.
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
        <TiersPageBody />
      </main>

      <footer className="border-t border-ink/10 py-8">
        <div className="mx-auto max-w-[1200px] px-6 text-xs text-ink/40">
          Every number on this page is read live from this merchant's current thresholds
          (<code className="font-mono">/insights/tier-config</code>) — cross-reference against{" "}
          <code className="font-mono">app/tiering.py</code>, not the other way round.
        </div>
      </footer>
    </div>
  );
}

function TiersPageBody() {
  const { data: config, isLoading, isError } = useTierConfig();

  if (isLoading) {
    return <p className="text-center text-ink/40 text-sm py-16">Loading current tier thresholds…</p>;
  }
  if (isError || !config) {
    return (
      <div className="flex items-center justify-center gap-2 text-center text-declined text-sm py-16">
        <AlertCircle className="size-4" /> Couldn't load tier config from the backend. Is the server running?
      </div>
    );
  }

  return (
    <>
      <section id="tiers" className="scroll-mt-24">
        <SectionHeading
          eyebrow="01 — The five tiers"
          title="NEW and RISK are states, not rungs"
          description="CASUAL → REGULAR → LOYAL is the climbable ladder, driven by the engagement score below. NEW ('no history yet') and RISK ('recovery effort not warranted') sit deliberately off that ladder — neither is reachable by scoring slightly better or worse."
        />
        <TierLadder config={config} />
      </section>

      <section id="formula" className="scroll-mt-24">
        <SectionHeading
          eyebrow="02 — The score formula"
          title="Five weighted components, one 0–100 number"
          description="Tiering used to be based on raw payment success rate — and it actively punished the right behaviour, since every retry mints a fresh order row (two failures then a success read as a 33% success rate). The engagement score instead measures behaviour the customer actually controls."
        />
        <ScoreFormula config={config} />
        <div className="grid sm:grid-cols-2 gap-3 mt-6 text-[13px] text-ink/45 leading-relaxed">
          <p className="rounded-xl border border-dashed border-ink/15 p-3">
            <strong className="text-ink/60">Retries collapse into one attempt.</strong> FAILED, FAILED, CAPTURED for
            the same basket is one successful purchase attempt, not two failures and a success — the single most
            important correction this formula makes over the old success-rate model.
          </p>
          <p className="rounded-xl border border-dashed border-ink/15 p-3">
            <strong className="text-ink/60">Cancels and abandons are windowed, not all-time.</strong> Behaviour
            outside the {config.tier_thresholds.tier_behavior_window_days}-day window stops counting entirely, so
            one bad month doesn't follow a customer forever.
          </p>
        </div>
      </section>

      <section id="benefits" className="scroll-mt-24">
        <SectionHeading
          eyebrow="03 — Benefits, opportunities & incentives"
          title="What each tier is actually worth"
          description="Discount bands, order-value caps and 30-day frequency caps are all per-tier and all runtime-tunable — the numbers below are this merchant's live values, not app defaults. Where a customer lands inside their own tier's score band decides where they land inside that tier's discount band."
        />
        <TierBenefitsGrid config={config} />
        <div className="grid sm:grid-cols-2 gap-3 mt-6 text-[13px] text-ink/45 leading-relaxed">
          <p className="rounded-xl border border-dashed border-ink/15 p-3">
            <strong className="text-ink/60">Two guardrails are never tunable.</strong> New and Risk can't become
            incentive-eligible no matter how the config is set, and the discount a customer sees always comes from
            the deterministic band formula — the LLM never chooses a money amount, on this path or any other.
          </p>
          <p className="rounded-xl border border-dashed border-ink/15 p-3">
            <strong className="text-ink/60">Frequency caps are deliberately inverted</strong> against the discount
            bands: Loyal gets the biggest discount but the fewest shots at it per 30 days, Casual the smallest
            discount but the most. If all three scaled up together, the tier with the most room to exploit them
            would get all three advantages at once.
          </p>
        </div>
      </section>
    </>
  );
}

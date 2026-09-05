import type { ComponentType } from "react";
import { Bell, CheckCircle2, Clock, IndianRupee, Repeat2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { TierConfig } from "@/lib/api/insights";

type Component = {
  key: keyof TierConfig["engagement_weights"];
  icon: ComponentType<{ className?: string }>;
  title: string;
  formula: string;
  rationale: string;
};

const COMPONENTS: Component[] = [
  {
    key: "completion",
    icon: CheckCircle2,
    title: "Completion",
    formula: "successful attempts ÷ (successful + attributable-failed + cancels + abandons)",
    rationale:
      "Only counts failures the customer could actually control. A network error, gateway timeout or issuer decline never enters this ratio at all — it isn't excused, it's excluded.",
  },
  {
    key: "frequency",
    icon: Repeat2,
    title: "Frequency",
    formula: "successful attempts ÷ (tenure in months × target purchases/month)",
    rationale:
      "Purchases per month of tenure, against what the merchant considers a regular buyer — not a raw count, so a brand-new customer isn't compared unfairly to a year-old one.",
  },
  {
    key: "monetary",
    icon: IndianRupee,
    title: "Monetary",
    formula: "avg captured order value ÷ target order value",
    rationale: "Average basket size against the merchant's own target — clamped at 1.0, so one huge order can't max the score alone.",
  },
  {
    key: "recency",
    icon: Clock,
    title: "Recency",
    formula: "1 − (days since last purchase ÷ recency window)",
    rationale: "Decays to 0 across the window since the last purchase. A customer who never purchased scores 0 here, not undefined.",
  },
  {
    key: "responsiveness",
    icon: Bell,
    title: "Responsiveness",
    formula: "nudges resumed ÷ (nudges resumed + nudges ignored)",
    rationale:
      "The one component that predicts whether spending recovery effort here will pay off. Defaults to a neutral 0.5 when never nudged — scoring 0 would punish a customer for never having needed recovery.",
  },
];

/** Formula weights are hardcoded (tiering.py's ENGAGEMENT_WEIGHTS) — an internal scoring
 * calibration, not a merchant-tunable knob — but the target/window values each component is
 * measured against ARE runtime-tunable, so those numbers come from the live config. */
export function ScoreFormula({ config }: { config: TierConfig }) {
  const { engagement_weights: w, tier_thresholds: t } = config;
  const liveValue: Record<Component["key"], string> = {
    completion: `windowed ${t.tier_behavior_window_days}d for cancels/abandons`,
    frequency: `target ${t.tier_target_orders_per_month}/mo`,
    monetary: `target ₹${t.tier_target_aov_inr}`,
    recency: `${t.tier_recency_window_days}-day window`,
    responsiveness: "neutral 0.5 if never nudged",
  };

  return (
    <div className="space-y-5">
      <div>
        <div className="flex h-9 rounded-full overflow-hidden border border-ink/10">
          {COMPONENTS.map((c, i) => (
            <div
              key={c.key}
              className={cn("h-full flex items-center justify-center", i % 2 === 0 ? "bg-ink/85" : "bg-ink/60")}
              style={{ width: `${w[c.key]}%` }}
            >
              <span className="text-[12.5px] font-semibold text-cream whitespace-nowrap px-1">{w[c.key]}%</span>
            </div>
          ))}
        </div>
        <p className="text-[12.5px] text-ink/40 mt-2">
          Rounded sum of all five, 0–100. Weights are fixed on purpose — see the callout below.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
        {COMPONENTS.map((c) => (
          <div key={c.key} className="rounded-2xl bg-white border border-ink/10 p-3.5">
            <div className="flex items-center gap-2 mb-2">
              <span className="size-7 rounded-lg bg-ink/8 text-ink/60 grid place-items-center shrink-0">
                <c.icon className="size-4" />
              </span>
              <div className="min-w-0">
                <p className="font-display font-semibold text-[15px] leading-tight">{c.title}</p>
                <p className="text-[12px] text-ink/40 font-mono">{w[c.key]}% weight</p>
              </div>
            </div>
            <p className="text-[12.5px] font-mono text-ink/55 leading-snug bg-ink/[0.03] rounded-md px-1.5 py-1.5 mb-2.5">
              {c.formula}
            </p>
            <p className="text-[13px] text-ink/55 leading-snug">{c.rationale}</p>
            <p className="text-[12px] text-ember font-semibold mt-2.5 pt-2.5 border-t border-ink/5">{liveValue[c.key]}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

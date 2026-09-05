import type { ComponentType } from "react";
import { ShieldAlert, Sparkles } from "lucide-react";

import { cn } from "@/lib/utils";
import { tierTone } from "@/lib/constants/tiers";
import { toneClasses, type Tone } from "@/lib/constants/tone";
import type { TierConfig } from "@/lib/api/insights";

const LADDER_TIERS = ["casual", "regular", "loyal"] as const;

/** The five tiers, drawn the way CustomerTier's own docstring frames them: NEW and RISK are
 * states you can be in, not rungs on a ladder — so they're rendered off to the sides, never as
 * segments of the 0-100 score rail that CASUAL/REGULAR/LOYAL actually live on. */
export function TierLadder({ config }: { config: TierConfig }) {
  const { tier_thresholds: t, tier_distribution: dist } = config;
  const regularPct = t.tier_regular_score;
  const loyalPct = t.tier_loyal_score;

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 lg:grid-cols-[220px_1fr] gap-4 items-stretch">
        <StateCard
          icon={Sparkles}
          tone={tierTone("new")}
          title="New"
          count={dist["new"] ?? 0}
          description="0 purchase attempts yet. Every customer starts here — the only way in, and not reachable from anywhere else on this page."
        />

        <div className="rounded-2xl bg-white border border-ink/10 p-5">
          <p className="text-[13px] uppercase tracking-wide text-ink/40 font-semibold mb-4">
            The climbable ladder — one 0–100 engagement score decides the rung
          </p>
          <div className="relative h-10 rounded-full overflow-hidden flex border border-ink/10">
            {LADDER_TIERS.map((tier) => {
              const c = toneClasses(tierTone(tier));
              const width =
                tier === "casual" ? regularPct : tier === "regular" ? loyalPct - regularPct : 100 - loyalPct;
              return (
                <div
                  key={tier}
                  className={cn("h-full flex items-center justify-center transition-all", c.badge)}
                  style={{ width: `${Math.max(width, 0)}%` }}
                >
                  <span className={cn("text-sm font-semibold capitalize whitespace-nowrap", c.text)}>
                    {tier}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="relative h-5 mt-2 text-[12.5px] text-ink/40 font-mono">
            <span className="absolute left-0">0</span>
            <span className="absolute -translate-x-1/2" style={{ left: `${regularPct}%` }}>
              {regularPct}
            </span>
            <span className="absolute -translate-x-1/2" style={{ left: `${loyalPct}%` }}>
              {loyalPct}
            </span>
            <span className="absolute right-0">100</span>
          </div>

          <div className="grid grid-cols-3 gap-3 mt-5">
            {LADDER_TIERS.map((tier) => {
              const c = toneClasses(tierTone(tier));
              const minAttempts =
                tier === "loyal"
                  ? t.tier_min_attempts_for_loyal
                  : tier === "regular"
                    ? t.tier_min_attempts_for_regular
                    : 0;
              return (
                <div key={tier} className="text-center">
                  <p className={cn("text-2xl font-extrabold", c.text)}>{dist[tier] ?? 0}</p>
                  <p className="text-[12.5px] text-ink/40 leading-tight mt-1">
                    {tier === "casual" ? "no volume floor" : `min ${minAttempts} purchase${minAttempts === 1 ? "" : "s"}`}
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <StateCard
        icon={ShieldAlert}
        tone={tierTone("risk")}
        title="Risk"
        count={dist["risk"] ?? 0}
        wide
        description={
          <>
            Checked <strong className="text-ink/70">before</strong> the ladder above — an enforcement gate, not the
            bottom rung, so it's never reachable by simply scoring low. Three independent triggers, each needing a
            minimum sample first:
            <ul className="mt-2 space-y-1 list-disc list-inside">
              <li>
                a permanent <code className="font-mono text-[13px]">risk_block</code> flag on any past payment
                (never self-corrects)
              </li>
              <li>
                over <strong>{Math.round(t.tier_risk_attributable_failure_rate * 100)}%</strong> of purchase
                attempts failing for a customer-side reason, once there are ≥{t.tier_risk_min_attempts} attempts
              </li>
              <li>
                over <strong>{Math.round(t.tier_risk_cancel_rate * 100)}%</strong> of checkout intents ending in an
                explicit cancel, once there are ≥{t.tier_risk_min_attempts}
              </li>
            </ul>
          </>
        }
      />
    </div>
  );
}

function StateCard({
  icon: Icon,
  tone,
  title,
  count,
  description,
  wide = false,
}: {
  icon: ComponentType<{ className?: string }>;
  tone: Tone;
  title: string;
  count: number;
  description: React.ReactNode;
  wide?: boolean;
}) {
  const c = toneClasses(tone);
  return (
    <div className={cn("rounded-2xl border border-dashed border-ink/20 bg-ink/[0.02] p-5", wide && "w-full")}>
      <div className="flex items-center gap-2 mb-3">
        <span className={cn("size-8 rounded-lg grid place-items-center shrink-0", c.badge)}>
          <Icon className="size-5" />
        </span>
        <p className="font-display font-semibold text-base">{title}</p>
        <span className={cn("ml-auto text-[12.5px] font-semibold px-2 py-0.5 rounded-full shrink-0", c.badge)}>
          {count} customer{count === 1 ? "" : "s"}
        </span>
      </div>
      <div className="text-sm text-ink/55 leading-relaxed">{description}</div>
    </div>
  );
}

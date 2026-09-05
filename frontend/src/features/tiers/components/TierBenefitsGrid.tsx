import { cn } from "@/lib/utils";
import { tierTone } from "@/lib/constants/tiers";
import { toneClasses } from "@/lib/constants/tone";
import type { TierConfig } from "@/lib/api/insights";

const ALL_TIERS = ["new", "casual", "regular", "loyal", "risk"] as const;
type TierKey = (typeof ALL_TIERS)[number];

const DESCRIPTIONS: Record<TierKey, string> = {
  new: "No completed purchase attempts yet. Every customer starts here.",
  casual: "Below the Regular score threshold, or hasn't hit the volume floor yet.",
  regular: "Cleared the Regular score and the minimum purchase-attempt floor.",
  loyal: "Cleared the Loyal score and its (higher) attempt floor — the top of the ladder.",
  risk: "Tripped the risk gate. Checked before the ladder, so it's never a consequence of a low score alone.",
};

const POSTURE: Record<TierKey, string> = {
  new: "Standard recovery treatment — reminders, resume links — same as any tier without incentive history to draw on yet.",
  casual: "Smallest discount band, but the most shots at it per 30 days.",
  regular: "Middle of both bands — a meaningfully bigger discount than Casual, offered less often.",
  loyal: "Biggest discount, highest order-value cap — but fewest shots per 30 days. Deliberately inverted from Casual.",
  risk: "Recovery effort is withheld or routed to a human — the account isn't a target for automated incentive spend.",
};

/** Five cards, one per CustomerTier value. Never a FlowNode/FlowTone card — tone comes from the
 * same tierTone()/toneClasses() pairing every tier badge elsewhere in the app already uses, so
 * "Loyal" renders in the exact same color here as on the dashboard's customer table. */
export function TierBenefitsGrid({ config }: { config: TierConfig }) {
  const { incentive_config: inc, tier_distribution: dist } = config;
  const eligible = new Set(inc.incentive_eligible_tiers);

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
      {ALL_TIERS.map((tier) => (
        <TierCard key={tier} tier={tier} count={dist[tier] ?? 0} isLadderTier={eligible.has(tier)} inc={inc} />
      ))}
    </div>
  );
}

function TierCard({
  tier,
  count,
  isLadderTier,
  inc,
}: {
  tier: TierKey;
  count: number;
  isLadderTier: boolean;
  inc: TierConfig["incentive_config"];
}) {
  const c = toneClasses(tierTone(tier));
  const hardBlocked = tier === "new" || tier === "risk";
  const band = inc.incentive_pct_bands[tier];
  const cap = inc.incentive_amount_caps_by_tier[tier];
  const freq = inc.incentive_max_per_30d_by_tier[tier];

  return (
    <div className="rounded-2xl bg-white border border-ink/10 p-4 flex flex-col">
      <div className="flex items-center justify-between gap-2 mb-2.5">
        <span className={cn("inline-flex rounded-full px-2.5 py-1 text-[13px] font-semibold capitalize", c.badge)}>
          {tier}
        </span>
        <span className="text-[12px] text-ink/40 font-medium">
          {count} customer{count === 1 ? "" : "s"}
        </span>
      </div>

      <p className="text-[13.5px] text-ink/55 leading-snug mb-3">{DESCRIPTIONS[tier]}</p>

      <div className="mt-auto space-y-2 text-[13px] pt-3 border-t border-ink/5">
        {hardBlocked && (
          <p className="text-ink/40 font-semibold">Never incentive-eligible — hardcoded, not a config toggle.</p>
        )}
        {!hardBlocked && !isLadderTier && (
          <p className="text-declined font-semibold">Currently toggled off for this tier (incentive_eligible_tiers).</p>
        )}
        {!hardBlocked && isLadderTier && band && (
          <>
            <Row label="Discount band" value={`${band[0]}–${band[1]}%`} />
            <Row label="Order-value cap" value={cap != null ? `₹${cap}` : "—"} />
            <Row label="Frequency cap" value={freq != null ? `${freq} / 30d` : "—"} />
          </>
        )}
      </div>

      <p className="text-[12.5px] text-ink/45 leading-snug mt-3">{POSTURE[tier]}</p>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-ink/40">{label}</span>
      <span className="font-mono font-semibold text-ink/70">{value}</span>
    </div>
  );
}

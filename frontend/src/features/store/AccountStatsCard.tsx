import { TIER_LABEL } from "@/lib/copy";
import type { CustomerStats } from "@/lib/api/auth";

export function AccountStatsCard({ stats }: { stats: CustomerStats }) {
  return (
    <div className="rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-4">
      <p className="font-display font-semibold text-sm mb-3">Your account</p>
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-ink/60">
        <div>
          Purchases: <b className="text-ink">{stats.total_orders}</b>
        </div>
        <div>
          Completed: <b className="text-soft">{stats.successful_orders}</b>
        </div>
        <div>
          Unfinished: <b className="text-failed">{stats.failed_orders}</b>
        </div>
        <div>
          Engagement score: <b className="text-ink">{stats.engagement_score ?? "—"}</b>/100
        </div>
      </div>
      {stats.tier_reason && <p className="text-xs text-ink/40 mt-2">{stats.tier_reason}</p>}
      {stats.next_tier && (
        <div className="mt-3 pt-3 border-t border-ink/10 text-xs text-ink/60">
          Next tier: <b className="text-ink">{TIER_LABEL[stats.next_tier.tier] ?? stats.next_tier.tier}</b> —{" "}
          {stats.next_tier.score_gap > 0
            ? `${stats.next_tier.score_gap} more engagement points`
            : "engagement score already there"}
          {stats.next_tier.attempts_gap > 0 &&
            `, and ${stats.next_tier.attempts_gap} more completed purchase${stats.next_tier.attempts_gap === 1 ? "" : "s"}`}
          .
        </div>
      )}
    </div>
  );
}

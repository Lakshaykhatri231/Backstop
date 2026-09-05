import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { applySuggestionForRecommendation, getIncentiveAnalysis, type AnalysisRange, type AnalysisResult, type Recommendation } from "@/lib/api/insights";
import { tierTone } from "@/lib/constants/tiers";
import { toneClasses } from "@/lib/constants/tone";
import { useInvalidateAuditLog } from "@/lib/hooks/useDashboardData";

import { AnalysisModalShell } from "./AnalysisModalShell";
import { RangeSelector } from "./shared/RangeSelector";
import { PatternsList } from "./shared/PatternsList";
import { RecommendationCard } from "./shared/RecommendationCard";

type TierLeaderboardRow = {
  tier: string;
  net_recovered_inr: number;
  redemption_rate_pct: number | null;
  baseline_conversion_rate_pct: number | null;
  avg_incentive_pct_given: number | null;
  incentive_pct_band: [number, number] | null;
  low_sample?: boolean;
};

type IncentiveOverview = {
  totals: { net_recovered_inr: number; discount_given_inr: number; revenue_recovered_inr: number; incentive_offered: number };
  tier_leaderboard: TierLeaderboardRow[];
  best_tier?: string;
  worst_tier?: string;
};

type IncentiveBucket = {
  tier: string;
  event_type: string;
  incentive_redemption_rate_pct: number | null;
  reminder_or_resume_link_conversion_rate_pct: number | null;
  net_recovered_inr: number;
  sample_size: number;
  incentive_offered: number;
  incentive_redeemed: number;
  reminder_or_resume_link_offered: number;
  reminder_or_resume_link_converted: number;
  discount_given_inr: number;
  revenue_recovered_inr: number;
  avg_incentive_pct_given: number | null;
  freq_cap_blocked_count: number;
  amount_cap_blocked_count: number;
  tier_gate_blocked_count: number;
  low_sample?: boolean;
};

const TRIGGER_META: Record<string, { label: string; icon: string }> = {
  silent_abandon: { label: "Silent abandon", icon: "🕐" },
  explicit_cancel: { label: "Explicit cancel", icon: "✕" },
};

function LiftBars({ incentiveRate, baselineRate, tone }: { incentiveRate: number | null; baselineRate: number | null; tone: string }) {
  const iw = incentiveRate == null ? 0 : Math.max(incentiveRate, incentiveRate > 0 ? 3 : 0);
  const bw = baselineRate == null ? 0 : Math.max(baselineRate, baselineRate > 0 ? 3 : 0);
  return (
    <div className="flex flex-col gap-1 flex-1 min-w-[140px]">
      <div className="flex items-center gap-1.5">
        <div className="flex-1 h-1.5 bg-ink/10 rounded overflow-hidden">
          <div className={`h-full rounded ${tone}`} style={{ width: `${iw}%` }} />
        </div>
        <span className="text-[10.5px] font-bold w-9 text-right">{incentiveRate == null ? "—" : `${incentiveRate}%`}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <div className="flex-1 h-1.5 bg-ink/10 rounded overflow-hidden">
          <div className="h-full rounded bg-ink/30" style={{ width: `${bw}%` }} />
        </div>
        <span className="text-[10.5px] text-ink/40 font-semibold w-9 text-right">{baselineRate == null ? "—" : `${baselineRate}%`}</span>
      </div>
    </div>
  );
}

export function IncentiveAnalysisModal({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [range, setRange] = useState<AnalysisRange>("30d");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [applied, setApplied] = useState<Record<number, boolean>>({});
  const [expandedBucket, setExpandedBucket] = useState<number | null>(null);
  const invalidateAuditLog = useInvalidateAuditLog();

  const applyMutation = useMutation({
    mutationFn: ({ rec, seq }: { rec: Recommendation; seq: number | undefined }) => applySuggestionForRecommendation(rec, seq),
  });

  async function analyze() {
    setLoading(true);
    setResult(null);
    setApplied({});
    const r = await getIncentiveAnalysis(range);
    setResult(r);
    setLoading(false);
  }

  async function implement(rec: Recommendation, i: number) {
    await applyMutation.mutateAsync({ rec, seq: result?.audit_sequence_num });
    setApplied((a) => ({ ...a, [i]: true }));
    invalidateAuditLog();
  }

  const overview = result?.["overview"] as IncentiveOverview | undefined;
  const buckets = (result?.["buckets"] as IncentiveBucket[] | undefined) ?? [];
  const maxAbsNet = Math.max(1, ...(overview?.tier_leaderboard ?? []).map((r) => Math.abs(r.net_recovered_inr)));
  const roiMultiple = overview && overview.totals.discount_given_inr > 0 ? overview.totals.revenue_recovered_inr / overview.totals.discount_given_inr : null;

  return (
    <AnalysisModalShell open={open} onOpenChange={onOpenChange} title="📊 Incentive Analysis">
      <RangeSelector range={range} onChange={setRange} onAnalyze={analyze} loading={loading} accentClass="bg-soft/15 text-soft" />

      {!result && !loading && <p className="text-center text-ink/40 text-sm py-8">Pick a time range and click Analyze.</p>}
      {loading && <p className="text-center text-ink/40 text-sm py-8">Crunching cart events for this range...</p>}

      {result && (
        <div>
          {result.summary && <div className="bg-white border border-ink/10 rounded-lg p-3 mb-4 text-sm">{result.summary}</div>}
          {result.llm_error && (
            <div className="bg-declined/10 border border-declined/40 rounded-lg p-3 mb-4 text-xs text-declined">
              Recommendations unavailable ({result.llm_error}). Metrics below are still accurate.
            </div>
          )}

          {overview && (
            <>
              <div className="bg-white border border-ink/10 rounded-lg p-4 mb-4 text-center">
                <p className="text-[11px] text-ink/40 uppercase tracking-wide mb-1">Net recovered this period</p>
                <p className={`font-display text-3xl font-bold ${overview.totals.net_recovered_inr >= 0 ? "text-soft" : "text-failed"}`}>
                  ₹{overview.totals.net_recovered_inr.toLocaleString("en-IN")}
                </p>
                <p className="text-xs text-ink/50 mt-1.5">
                  {roiMultiple == null
                    ? "No discounts redeemed yet this period"
                    : `₹${roiMultiple.toFixed(2)} recovered for every ₹1 given away · ${overview.totals.incentive_offered} discount${overview.totals.incentive_offered === 1 ? "" : "s"} offered`}
                </p>
              </div>

              <PatternsList patterns={result.patterns} />

              <h3 className="text-sm font-bold mb-2.5">Which tier recovered best</h3>
              <div className="bg-white border border-ink/10 rounded-lg p-3.5 mb-4">
                {overview.tier_leaderboard.length === 0 ? (
                  <p className="text-xs text-ink/40">No incentive-eligible activity in this range.</p>
                ) : (
                  overview.tier_leaderboard.map((row) => {
                    const c = toneClasses(tierTone(row.tier));
                    return (
                      <div key={row.tier} className="mb-3">
                        <div className="flex justify-between items-center mb-1">
                          <div className="flex items-center gap-1.5">
                            <span className={`inline-flex rounded px-2 py-0.5 text-[11px] font-semibold ${c.badge}`}>{row.tier}</span>
                            {row.tier === overview.best_tier && <span className="text-[11px] text-soft font-semibold">best</span>}
                            {row.tier === overview.worst_tier && <span className="text-[11px] text-failed font-semibold">worst</span>}
                            {row.low_sample && <span className="text-[11px] text-declined font-semibold">low sample</span>}
                          </div>
                          <span className={`text-sm font-bold ${row.net_recovered_inr >= 0 ? "text-soft" : "text-failed"}`}>
                            ₹{row.net_recovered_inr.toLocaleString("en-IN")} net
                          </span>
                        </div>
                        <div className="h-1.5 bg-ink/10 rounded overflow-hidden mb-1">
                          <div className={`h-full rounded ${c.dot}`} style={{ width: `${Math.max(4, (Math.abs(row.net_recovered_inr) / maxAbsNet) * 100)}%` }} />
                        </div>
                        <p className="text-[11.5px] text-ink/50">
                          {row.redemption_rate_pct == null ? "no discounts taken yet" : `${row.redemption_rate_pct}% took the discount`}
                          {row.baseline_conversion_rate_pct != null && ` (a plain reminder alone converts ${row.baseline_conversion_rate_pct}%)`}
                          {row.avg_incentive_pct_given != null && row.incentive_pct_band && (
                            <> · typically offered {row.avg_incentive_pct_given}%, this tier's range is {row.incentive_pct_band[0]}–{row.incentive_pct_band[1]}%</>
                          )}
                        </p>
                      </div>
                    );
                  })
                )}
              </div>
            </>
          )}

          <h3 className="text-sm font-bold mb-2 text-ink/60">Full breakdown, by tier and trigger</h3>
          {buckets.length === 0 ? (
            <p className="text-xs text-ink/40 py-4">No cart events in this range.</p>
          ) : (
            <div className="mb-5">
              {buckets.map((b, i) => {
                const isExp = expandedBucket === i;
                const trigger = TRIGGER_META[b.event_type] ?? { label: b.event_type.replace(/_/g, " "), icon: "•" };
                const c = toneClasses(tierTone(b.tier));
                return (
                  <div key={i} className="bg-white border border-ink/10 rounded-lg px-3.5 py-2.5 mb-2">
                    <div className="flex items-center gap-2.5 flex-wrap">
                      <div className="flex items-center gap-1.5 min-w-[170px]">
                        <span className={`inline-flex rounded px-2 py-0.5 text-[11px] font-semibold ${c.badge}`}>{b.tier}</span>
                        <span className="text-xs text-ink/50 whitespace-nowrap">
                          {trigger.icon} {trigger.label}
                        </span>
                        {b.low_sample && <span className="text-[11px] text-declined font-semibold">low sample</span>}
                      </div>
                      <LiftBars incentiveRate={b.incentive_redemption_rate_pct} baselineRate={b.reminder_or_resume_link_conversion_rate_pct} tone={c.dot} />
                      <div className="text-right min-w-[90px]">
                        <p className={`text-sm font-extrabold ${b.net_recovered_inr >= 0 ? "text-soft" : "text-failed"}`}>
                          ₹{b.net_recovered_inr.toLocaleString("en-IN")}
                        </p>
                        <p className="text-[10px] text-ink/40">net · {b.sample_size} events</p>
                      </div>
                      <button onClick={() => setExpandedBucket(isExp ? null : i)} className="text-[11px] text-indigo font-semibold whitespace-nowrap">
                        {isExp ? "Hide details ▾" : "Details ▸"}
                      </button>
                    </div>
                    {isExp && (
                      <div className="mt-2.5 pt-2.5 border-t border-ink/10 flex gap-5 flex-wrap text-[11.5px]">
                        <div>
                          <span className="text-ink/40">Incentive offered/redeemed: </span>
                          {b.incentive_offered}/{b.incentive_redeemed}
                        </div>
                        <div>
                          <span className="text-ink/40">Reminder offered/converted: </span>
                          {b.reminder_or_resume_link_offered}/{b.reminder_or_resume_link_converted}
                        </div>
                        <div>
                          <span className="text-ink/40">Given away: </span>
                          <span className="text-declined">₹{b.discount_given_inr.toLocaleString("en-IN")}</span>
                        </div>
                        <div>
                          <span className="text-ink/40">Recovered: </span>
                          <span className="text-soft">₹{b.revenue_recovered_inr.toLocaleString("en-IN")}</span>
                        </div>
                        {b.avg_incentive_pct_given != null && (
                          <div>
                            <span className="text-ink/40">Avg % given: </span>
                            {b.avg_incentive_pct_given}%
                          </div>
                        )}
                        {(b.freq_cap_blocked_count > 0 || b.amount_cap_blocked_count > 0 || b.tier_gate_blocked_count > 0) && (
                          <div>
                            <span className="text-ink/40">No discount offered: </span>
                            {[
                              b.freq_cap_blocked_count > 0 && `${b.freq_cap_blocked_count} already at their monthly limit`,
                              b.amount_cap_blocked_count > 0 && `${b.amount_cap_blocked_count} cart over the value cap`,
                              b.tier_gate_blocked_count > 0 && `${b.tier_gate_blocked_count} tier not eligible`,
                            ]
                              .filter(Boolean)
                              .join(", ")}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          <h3 className="text-sm font-bold mb-2 text-ink/60">Recommendations</h3>
          {result.recommendations.length === 0 && !result.llm_error && <p className="text-xs text-ink/40">No changes recommended from this data.</p>}
          {result.recommendations.map((rec, i) => (
            <RecommendationCard key={i} rec={rec} applied={!!applied[i]} applying={applyMutation.isPending} onImplement={() => implement(rec, i)} />
          ))}
        </div>
      )}
    </AnalysisModalShell>
  );
}

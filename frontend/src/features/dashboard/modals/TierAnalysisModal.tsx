import { type ReactNode, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import {
  applySuggestionForRecommendation,
  commitTierReevaluation,
  getTierAnalysis,
  getTierReevaluationPreview,
  type AnalysisRange,
  type AnalysisResult,
  type Recommendation,
  type TierReevaluationPreview,
} from "@/lib/api/insights";
import { tierTone } from "@/lib/constants/tiers";
import { toneClasses } from "@/lib/constants/tone";
import { paramLabel } from "@/lib/constants/params";
import { useInvalidateAuditLog } from "@/lib/hooks/useDashboardData";

import { AnalysisModalShell } from "./AnalysisModalShell";
import { RangeSelector } from "./shared/RangeSelector";
import { PatternsList } from "./shared/PatternsList";

type TierPerfRow = {
  tier: string;
  successful_orders: number;
  failed_orders: number;
  revenue_captured_inr: number;
  revenue_lost_inr: number;
  incentives_offered: number;
  discount_given_inr: number;
  net_gain_inr: number;
  cancellations: number;
  avg_order_value_inr: number | null;
};
type NearMissCustomer = { id: string; name: string | null; email: string; engagement_score?: number; purchases_needed?: number; next_tier?: string; points_needed?: number; trigger?: string };
type NearMissCustomers = { close_to_promotion: NearMissCustomer[]; close_on_score: NearMissCustomer[]; close_to_risk: NearMissCustomer[] };
type RiskFlagRedemption = { total_flagged_permanently: number; redeemed_since_count: number; redeemed_customers: (NearMissCustomer & { successful_orders_since: number })[] };
type DormantRow = { dormant: number; total: number; dormant_pct: number | null };

const SHOW_LIMIT = 5;

function ExpandableList<T extends { id: string }>({
  items,
  renderItem,
  emptyLabel,
}: {
  items: T[] | undefined;
  renderItem: (c: T) => ReactNode;
  emptyLabel?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!items || items.length === 0) return <p className="text-[11.5px] text-ink/40">{emptyLabel ?? "None right now."}</p>;
  const visible = expanded ? items : items.slice(0, SHOW_LIMIT);
  const remaining = items.length - SHOW_LIMIT;
  return (
    <div>
      {visible.map((c) => (
        <div key={c.id}>{renderItem(c)}</div>
      ))}
      {remaining > 0 && (
        <button onClick={() => setExpanded((e) => !e)} className="text-[11px] font-semibold text-indigo mt-1">
          {expanded ? "Show less" : `Show ${remaining} more`}
        </button>
      )}
    </div>
  );
}

export function TierAnalysisModal({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [range, setRange] = useState<AnalysisRange>("30d");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [previews, setPreviews] = useState<Record<number, TierReevaluationPreview>>({});
  const [previewLoading, setPreviewLoading] = useState<number | null>(null);
  const [applying, setApplying] = useState<number | null>(null);
  const [appliedResults, setAppliedResults] = useState<Record<number, { changed: number; total_customers: number }>>({});
  const invalidateAuditLog = useInvalidateAuditLog();
  const applyMutation = useMutation({
    mutationFn: ({ rec, seq }: { rec: Recommendation; seq: number | undefined }) => applySuggestionForRecommendation(rec, seq),
  });

  async function analyze() {
    setLoading(true);
    setResult(null);
    setPreviews({});
    setAppliedResults({});
    const r = await getTierAnalysis(range);
    setResult(r);
    setLoading(false);
  }

  async function previewRec(rec: Recommendation, i: number) {
    setPreviewLoading(i);
    const r = await getTierReevaluationPreview(rec.param, String(rec.suggested_value));
    setPreviews((p) => ({ ...p, [i]: r }));
    setPreviewLoading(null);
  }

  function cancelPreview(i: number) {
    setPreviews((p) => {
      const next = { ...p };
      delete next[i];
      return next;
    });
  }

  async function applyRec(rec: Recommendation, i: number) {
    setApplying(i);
    await applyMutation.mutateAsync({ rec, seq: result?.audit_sequence_num });
    const commitResult = await commitTierReevaluation();
    setAppliedResults((a) => ({ ...a, [i]: commitResult }));
    setPreviews((p) => {
      const next = { ...p };
      delete next[i];
      return next;
    });
    setApplying(null);
    invalidateAuditLog();
  }

  const perf = (result?.["tier_wise_performance"] as TierPerfRow[] | undefined) ?? [];
  const dist = result?.["tier_distribution"] as Record<string, number> | undefined;
  const nm = result?.["near_miss_customers"] as NearMissCustomers | undefined;
  const rr = result?.["risk_flag_redemption"] as RiskFlagRedemption | undefined;
  const dormant = result?.["dormant_accounts_by_tier"] as Record<string, DormantRow> | undefined;

  return (
    <AnalysisModalShell open={open} onOpenChange={onOpenChange} title="🎯 Tier Analysis">
      <RangeSelector range={range} onChange={setRange} onAnalyze={analyze} loading={loading} accentClass="bg-loyal/15 text-loyal" />
      <p className="text-[11.5px] text-ink/40 mb-4">
        Tier-wise performance and dormant accounts reflect the selected range. Tier distribution, near-miss customers, and risk-flag
        redemption are always a snapshot of customers as they stand right now.
      </p>

      {!result && !loading && <p className="text-center text-ink/40 text-sm py-8">Pick a time range and click Analyze.</p>}
      {loading && <p className="text-center text-ink/40 text-sm py-8">Crunching customer tier data...</p>}

      {result && (
        <div>
          {result.summary && <div className="bg-white border border-ink/10 rounded-lg p-3 mb-4 text-sm">{result.summary}</div>}
          {result.llm_error && (
            <div className="bg-declined/10 border border-declined/40 rounded-lg p-3 mb-4 text-xs text-declined">
              Recommendations unavailable ({result.llm_error}). Metrics below are still accurate.
            </div>
          )}

          <PatternsList patterns={result.patterns} />

          <h3 className="text-sm font-bold mb-2.5">Tier-wise performance</h3>
          <div className="overflow-x-auto mb-6">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="text-ink/40 text-[11px]">
                  {["Tier", "Success", "Failed", "Captured ₹", "Lost ₹", "Incentives", "Discount ₹", "Net gain ₹", "Cancels", "Avg order ₹"].map((h) => (
                    <th key={h} className="px-2 py-1.5 border-b border-ink/10 whitespace-nowrap font-semibold">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {perf.map((row, i) => {
                  const c = toneClasses(tierTone(row.tier));
                  return (
                    <tr key={i} className="border-b border-ink/10">
                      <td className="px-2 py-1.5">
                        <span className={`inline-flex rounded px-2 py-0.5 text-[11px] font-semibold ${c.badge}`}>{row.tier}</span>
                      </td>
                      <td className="px-2 py-1.5 text-xs text-soft">{row.successful_orders}</td>
                      <td className="px-2 py-1.5 text-xs text-failed">{row.failed_orders}</td>
                      <td className="px-2 py-1.5 text-xs text-soft">₹{row.revenue_captured_inr}</td>
                      <td className="px-2 py-1.5 text-xs text-failed">₹{row.revenue_lost_inr}</td>
                      <td className="px-2 py-1.5 text-xs text-ink/40">{row.incentives_offered}</td>
                      <td className="px-2 py-1.5 text-xs text-declined">₹{row.discount_given_inr}</td>
                      <td className={`px-2 py-1.5 text-xs font-bold ${row.net_gain_inr >= 0 ? "text-soft" : "text-failed"}`}>₹{row.net_gain_inr}</td>
                      <td className="px-2 py-1.5 text-xs text-ink/40">{row.cancellations}</td>
                      <td className="px-2 py-1.5 text-xs text-ink/50">{row.avg_order_value_inr == null ? "—" : `₹${row.avg_order_value_inr}`}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <h3 className="text-sm font-bold mb-2.5">Tier distribution right now</h3>
          <div className="flex gap-5 mb-6 flex-wrap">
            {dist &&
              Object.entries(dist).map(([tier, count]) => {
                const c = toneClasses(tierTone(tier));
                return (
                  <div key={tier} className="text-center">
                    <p className={`text-2xl font-extrabold ${c.text}`}>{count}</p>
                    <span className={`inline-flex rounded px-2 py-0.5 text-[11px] font-semibold ${c.badge}`}>{tier}</span>
                  </div>
                );
              })}
          </div>

          <h3 className="text-sm font-bold mb-2.5">Customers on the edge</h3>
          <div className="flex gap-3 mb-6 flex-wrap">
            <div className="flex-1 min-w-[260px] bg-white border border-ink/10 rounded-lg p-3">
              <p className="text-xs font-bold text-soft mb-2">Score is there, need more purchases ({nm?.close_to_promotion.length ?? 0})</p>
              <ExpandableList
                items={nm?.close_to_promotion}
                renderItem={(c) => (
                  <div className="text-[11.5px] text-ink/50 py-0.5">
                    {c.name || c.email} <span className="text-ink/40">(score {c.engagement_score}, {c.purchases_needed} more → {c.next_tier})</span>
                  </div>
                )}
              />
            </div>
            <div className="flex-1 min-w-[260px] bg-white border border-ink/10 rounded-lg p-3">
              <p className="text-xs font-bold text-loyal mb-2">Within 10 points of the next tier ({nm?.close_on_score.length ?? 0})</p>
              <ExpandableList
                items={nm?.close_on_score}
                renderItem={(c) => (
                  <div className="text-[11.5px] text-ink/50 py-0.5">
                    {c.name || c.email} <span className="text-ink/40">(score {c.engagement_score}, +{c.points_needed} → {c.next_tier})</span>
                  </div>
                )}
              />
            </div>
            <div className="flex-1 min-w-[260px] bg-white border border-ink/10 rounded-lg p-3">
              <p className="text-xs font-bold text-failed mb-2">One bad event from Risk ({nm?.close_to_risk.length ?? 0})</p>
              <ExpandableList
                items={nm?.close_to_risk}
                renderItem={(c) => (
                  <div className="text-[11.5px] text-ink/50 py-0.5">
                    {c.name || c.email} <span className="text-ink/40">({c.trigger})</span>
                  </div>
                )}
              />
            </div>
          </div>

          <h3 className="text-sm font-bold mb-2.5">Were flagged customers treated too harshly?</h3>
          <div className="bg-white border border-ink/10 rounded-lg p-3.5 mb-6">
            {rr && rr.total_flagged_permanently > 0 ? (
              <>
                <p className="text-[12.5px] text-ink/50 mb-2.5">
                  <b className="text-ink">{rr.redeemed_since_count}</b> of <b className="text-ink">{rr.total_flagged_permanently}</b>{" "}
                  permanently-flagged customers have paid successfully since being flagged.
                </p>
                <ExpandableList
                  items={rr.redeemed_customers}
                  emptyLabel="None have paid successfully since being flagged."
                  renderItem={(c) => (
                    <div className="text-[11.5px] text-ink/50 py-0.5">
                      {c.name || c.email} — {c.successful_orders_since} successful order
                      {c.successful_orders_since === 1 ? "" : "s"} since being flagged
                    </div>
                  )}
                />
              </>
            ) : (
              <p className="text-xs text-ink/40">No customers are permanently flagged from a risk block.</p>
            )}
          </div>

          <h3 className="text-sm font-bold mb-2.5">Dormant accounts (no orders in this range)</h3>
          <div className="flex gap-5 mb-6 flex-wrap">
            {dormant &&
              Object.entries(dormant).map(([tier, d]) => {
                const c = toneClasses(tierTone(tier));
                return (
                  <div key={tier} className="text-center">
                    <p className={`text-xl font-extrabold ${c.text}`}>{d.dormant_pct == null ? "—" : `${d.dormant_pct}%`}</p>
                    <span className={`inline-flex rounded px-2 py-0.5 text-[11px] font-semibold ${c.badge}`}>{tier}</span>
                    <p className="text-[10.5px] text-ink/40 mt-0.5">
                      {d.dormant}/{d.total}
                    </p>
                  </div>
                );
              })}
          </div>

          <h3 className="text-sm font-bold mb-2 text-ink/60">Recommendations</h3>
          {result.recommendations.length > 0 && (
            <div className="bg-loyal/10 border border-loyal/40 rounded-lg p-3 mb-3 text-xs text-loyal">
              ⚠️ Every recommendation below changes how tiers are <b>computed</b>, not just future behavior. Applying one doesn't move any
              existing customer by itself — you'll get a chance to preview and confirm before anyone's tier actually changes.
            </div>
          )}
          {result.recommendations.length === 0 && !result.llm_error && <p className="text-xs text-ink/40">No changes recommended from this data.</p>}
          {result.recommendations.map((rec, i) => {
            const preview = previews[i];
            const applied = appliedResults[i];
            return (
              <div key={i} className="bg-white border border-loyal/40 rounded-lg p-3 mb-2.5">
                <div className="flex justify-between items-center gap-3 flex-wrap">
                  <div>
                    <span className="inline-flex rounded bg-loyal/15 text-loyal px-2 py-0.5 text-[11px] font-semibold">{paramLabel(rec.param)}</span>
                    <span className="ml-2.5 text-sm">
                      <span className="text-ink/40">{String(rec.current_value)}</span>
                      <span className="text-ink/50"> → </span>
                      <span className="text-soft font-bold">{String(rec.suggested_value)}</span>
                    </span>
                  </div>
                  {!preview && !applied && (
                    <button
                      onClick={() => previewRec(rec, i)}
                      disabled={previewLoading === i}
                      className="rounded-md px-3.5 py-1.5 text-xs font-bold bg-loyal/15 text-loyal whitespace-nowrap disabled:opacity-50"
                    >
                      {previewLoading === i ? "Checking..." : "Preview Impact"}
                    </button>
                  )}
                  {applied && <div className="rounded-md px-3.5 py-1.5 text-xs font-bold bg-soft/15 text-soft">✓ Applied</div>}
                </div>
                <p className="text-xs text-ink/50 mt-2">{rec.rationale}</p>
                {rec.supporting_metric && <p className="text-[11px] text-ink/40 mt-1">Based on: {rec.supporting_metric}</p>}

                {preview && !applied && (
                  <div className="mt-2.5 pt-2.5 border-t border-ink/10">
                    <p className="text-[12.5px] mb-2">
                      {preview.unchanged} of {preview.total_customers} customers unchanged.
                    </p>
                    {Object.entries(preview.moves).map(([move, data]) => (
                      <p key={move} className="text-xs text-ink/50 mb-1">
                        <b className="text-ink">{data.count}</b> customer{data.count === 1 ? "" : "s"}: {move.replace("->", "→")}
                      </p>
                    ))}
                    {Object.keys(preview.moves).length === 0 && <p className="text-xs text-ink/40 mb-2">No customer would actually move under this change.</p>}
                    <div className="flex gap-2 mt-2">
                      <button
                        onClick={() => applyRec(rec, i)}
                        disabled={applying === i}
                        className="rounded-md px-4 py-1.5 text-xs font-bold bg-soft text-white disabled:opacity-50"
                      >
                        {applying === i ? "Applying..." : "Apply"}
                      </button>
                      <button
                        onClick={() => cancelPreview(i)}
                        disabled={applying === i}
                        className="rounded-md px-4 py-1.5 text-xs font-bold border border-ink/15 text-ink/50 disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {applied && (
                  <div className="mt-2.5 pt-2.5 border-t border-ink/10 text-[12.5px] text-soft">
                    ✓ Done — {applied.changed} of {applied.total_customers} customers' tiers updated.
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </AnalysisModalShell>
  );
}

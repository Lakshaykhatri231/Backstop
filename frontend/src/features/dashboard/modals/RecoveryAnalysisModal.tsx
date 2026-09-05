import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { applySuggestionForRecommendation, getRecoveryAnalysis, type AnalysisRange, type AnalysisResult, type Recommendation } from "@/lib/api/insights";
import { tierTone } from "@/lib/constants/tiers";
import { toneClasses } from "@/lib/constants/tone";
import { useInvalidateAuditLog } from "@/lib/hooks/useDashboardData";

import { AnalysisModalShell } from "./AnalysisModalShell";
import { RangeSelector } from "./shared/RangeSelector";
import { PatternsList } from "./shared/PatternsList";
import { RecommendationCard } from "./shared/RecommendationCard";

type LeakSignal = {
  signal: string;
  label: string;
  count: number;
  amount_at_stake_inr: number;
  recovered_count: number;
  recovered_inr: number;
  still_open_count: number;
  still_open_inr: number;
  lost_count: number;
  lost_inr: number;
  recovery_rate_pct: number | null;
  handed_to_checkout_count: number;
};
type LeakSummary = { signals: LeakSignal[] };
type FailureReasonRow = { reason: string; count: number; recovery_rate_pct: number | null; lost_inr: number; low_sample?: boolean };
type GiveUpAnalysis = {
  count: number;
  amount_inr: number;
  avg_attempts_before_giving_up: number | null;
  lapsed_silently_count: number;
  by_reason: Record<string, number>;
};
type RetryStep = { attempt: number; runs_reaching: number; recovered: number; recovery_rate_pct: number | null };
type RetryEffectiveness = { max_auto_retries: number; avg_attempts: number | null; ladder: RetryStep[] };
type EscalationAmountAnalysis = { purely_amount_triggered: number; other_reason: number; near_floor_count: number; current_threshold_inr: number };
type ConfidenceOverride = { total_decisions: number; escalated_by_gate: number; escalated_rate_pct: number | null };
type AgentReliability = {
  llm_decisions: number;
  fallback_decisions: number;
  llm_escalation_rate_pct: number | null;
  rules_fallback_escalation_rate_pct: number | null;
};

function StatRow({ label, value, colorClass }: { label: string; value: string | number; colorClass?: string | undefined }) {
  return (
    <div className="flex justify-between py-1 text-[12.5px]">
      <span className="text-ink/40">{label}</span>
      <span className={`font-semibold ${colorClass ?? "text-ink"}`}>{value}</span>
    </div>
  );
}

export function RecoveryAnalysisModal({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [range, setRange] = useState<AnalysisRange>("30d");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [applied, setApplied] = useState<Record<number, boolean>>({});
  const invalidateAuditLog = useInvalidateAuditLog();
  const applyMutation = useMutation({
    mutationFn: ({ rec, seq }: { rec: Recommendation; seq: number | undefined }) => applySuggestionForRecommendation(rec, seq),
  });

  async function analyze() {
    setLoading(true);
    setResult(null);
    setApplied({});
    const r = await getRecoveryAnalysis(range);
    setResult(r);
    setLoading(false);
  }

  async function implement(rec: Recommendation, i: number) {
    await applyMutation.mutateAsync({ rec, seq: result?.audit_sequence_num });
    setApplied((a) => ({ ...a, [i]: true }));
    invalidateAuditLog();
  }

  const leak = result?.["leak_summary"] as LeakSummary | undefined;
  const reasons = (result?.["failure_reason_analysis"] as FailureReasonRow[] | undefined) ?? [];
  const giveUp = result?.["give_up_analysis"] as GiveUpAnalysis | undefined;
  const retry = result?.["retry_effectiveness"] as RetryEffectiveness | undefined;
  const esc = result?.["escalation_amount_analysis"] as EscalationAmountAnalysis | undefined;
  const cov = result?.["confidence_override_rates"] as Record<string, ConfidenceOverride> | undefined;
  const agent = result?.["agent_reliability"] as AgentReliability | undefined;
  const repeatOff = result?.["repeat_offenders_by_tier"] as Record<string, number> | undefined;

  const inr = (n: number) => `₹${Math.round(n || 0).toLocaleString("en-IN")}`;
  const handedTotal = leak ? leak.signals.reduce((sum, s) => sum + s.handed_to_checkout_count, 0) : 0;

  return (
    <AnalysisModalShell open={open} onOpenChange={onOpenChange} title="📉 Loss & Recovery Analysis">
      <RangeSelector range={range} onChange={setRange} onAnalyze={analyze} loading={loading} accentClass="bg-failed/15 text-failed" />
      <p className="text-[11.5px] text-ink/40 mb-4">
        Covers every way an order is lost: carts that go quiet, carts customers delete, payments that fail, and customers who give up on a
        failed payment.
      </p>

      {!result && !loading && <p className="text-center text-ink/40 text-sm py-8">Pick a time range and click Analyze.</p>}
      {loading && <p className="text-center text-ink/40 text-sm py-8">Going through abandoned carts, cancellations and failed payments...</p>}

      {result && (
        <div>
          {result.summary && <div className="bg-white border border-ink/10 rounded-lg p-3 mb-4 text-sm">{result.summary}</div>}
          {result.llm_error && (
            <div className="bg-declined/10 border border-declined/40 rounded-lg p-3 mb-4 text-xs text-declined">
              Recommendations unavailable ({result.llm_error}). Metrics below are still accurate.
            </div>
          )}

          <PatternsList patterns={result.patterns} />

          <h3 className="text-sm font-bold mb-1">Where you're losing orders</h3>
          <p className="text-[11.5px] text-ink/40 mb-2.5">
            The value of the carts and payments involved — not the same as the booked figures on the Revenue &amp; Customers tab, which only
            count money the ledger actually moved.
          </p>
          <div className="overflow-x-auto mb-2">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="text-ink/40 text-[11px]">
                  {["What happened", "How many", "Value at stake", "Came back", "Still in play", "Gone", "Came back?"].map((h) => (
                    <th key={h} className="px-2 py-1.5 border-b border-ink/10 whitespace-nowrap font-semibold">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {leak?.signals.map((s, i) => (
                  <tr key={i} className="border-b border-ink/10">
                    <td className="px-2 py-1.5 text-xs whitespace-nowrap">{s.label}</td>
                    <td className="px-2 py-1.5 text-xs text-ink/50">{s.count}</td>
                    <td className="px-2 py-1.5 text-xs whitespace-nowrap">{inr(s.amount_at_stake_inr)}</td>
                    <td className="px-2 py-1.5 text-xs text-soft whitespace-nowrap">
                      {s.recovered_count} · {inr(s.recovered_inr)}
                    </td>
                    <td className="px-2 py-1.5 text-xs text-declined whitespace-nowrap">
                      {s.still_open_count} · {inr(s.still_open_inr)}
                    </td>
                    <td className="px-2 py-1.5 text-xs text-failed whitespace-nowrap">
                      {s.lost_count} · {inr(s.lost_inr)}
                    </td>
                    <td className={`px-2 py-1.5 text-xs font-bold whitespace-nowrap ${s.recovery_rate_pct == null ? "text-ink/40" : s.recovery_rate_pct >= 40 ? "text-soft" : "text-declined"}`}>
                      {s.recovery_rate_pct == null ? "—" : `${s.recovery_rate_pct}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {handedTotal > 0 && (
            <p className="text-[11.5px] text-ink/40 mb-6">
              {handedTotal} cart{handedTotal === 1 ? " was" : "s were"} won back, but the payment then failed — that money is counted in the
              "Payment failed" row, never twice.
            </p>
          )}
          {handedTotal === 0 && <div className="mb-6" />}

          <h3 className="text-sm font-bold mb-2.5">Which of these are worth chasing</h3>
          <div className="bg-white border border-ink/10 rounded-lg p-3.5 mb-6">
            {(leak?.signals.filter((s) => s.recovery_rate_pct != null).length ?? 0) === 0 ? (
              <p className="text-xs text-ink/40">Nothing has resolved yet in this range.</p>
            ) : (
              leak?.signals
                .filter((s) => s.recovery_rate_pct != null)
                .map((s) => (
                  <div key={s.signal} className="mb-3">
                    <div className="flex justify-between text-[12.5px] mb-1">
                      <span className="font-semibold">{s.label}</span>
                      <span className="text-ink/50">
                        {s.recovery_rate_pct}% come back
                        <span className="text-ink/40"> ({s.recovered_count} of {s.recovered_count + s.lost_count})</span>
                      </span>
                    </div>
                    <div className="h-1.5 bg-ink/10 rounded overflow-hidden">
                      <div
                        className={`h-full rounded ${(s.recovery_rate_pct ?? 0) >= 40 ? "bg-soft" : "bg-declined"}`}
                        style={{ width: `${Math.max(3, s.recovery_rate_pct ?? 0)}%` }}
                      />
                    </div>
                  </div>
                ))
            )}
          </div>

          <h3 className="text-sm font-bold mb-2.5">Why payments fail, and which are winnable</h3>
          <div className="overflow-x-auto mb-6">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="text-ink/40 text-[11px]">
                  {["Reason", "How many", "Ever recovered", "Money gone"].map((h) => (
                    <th key={h} className="px-2 py-1.5 border-b border-ink/10 whitespace-nowrap font-semibold">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {reasons.map((r, i) => (
                  <tr key={i} className="border-b border-ink/10">
                    <td className="px-2 py-1.5 text-xs capitalize whitespace-nowrap">
                      {r.reason.replace(/_/g, " ")} {r.low_sample && <span className="text-declined ml-1.5">low sample</span>}
                    </td>
                    <td className="px-2 py-1.5 text-xs text-ink/50">{r.count}</td>
                    <td className={`px-2 py-1.5 text-xs whitespace-nowrap ${r.recovery_rate_pct == null ? "text-ink/40" : r.recovery_rate_pct > 0 ? "text-soft" : "text-failed"}`}>
                      {r.recovery_rate_pct == null ? "—" : `${r.recovery_rate_pct}%`}
                    </td>
                    <td className="px-2 py-1.5 text-xs text-failed whitespace-nowrap">{inr(r.lost_inr)}</td>
                  </tr>
                ))}
                {reasons.length === 0 && (
                  <tr>
                    <td colSpan={4} className="text-center py-4 text-ink/40 text-xs">
                      No failed payments in this range.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <h3 className="text-sm font-bold mb-1">When customers give up</h3>
          <p className="text-[11.5px] text-ink/40 mb-2.5">
            Giving up is how a failed payment ends — this money is already counted as gone in the "Payment failed" row above, not on top of it.
          </p>
          <div className="bg-white border border-ink/10 rounded-lg p-3.5 mb-6">
            {giveUp && giveUp.count > 0 ? (
              <div className="flex gap-5 flex-wrap">
                <div className="flex-1 min-w-[130px] text-center">
                  <p className="text-2xl font-extrabold text-failed">{giveUp.count}</p>
                  <p className="text-[11.5px] text-ink/50">said they're done</p>
                </div>
                <div className="flex-1 min-w-[130px] text-center">
                  <p className="text-2xl font-extrabold text-failed">{inr(giveUp.amount_inr)}</p>
                  <p className="text-[11.5px] text-ink/50">walked out the door</p>
                </div>
                <div className="flex-1 min-w-[130px] text-center">
                  <p className="text-2xl font-extrabold text-declined">{giveUp.avg_attempts_before_giving_up ?? "—"}</p>
                  <p className="text-[11.5px] text-ink/50">tries first, on average</p>
                </div>
                <div className="flex-1 min-w-[130px] text-center">
                  <p className="text-2xl font-extrabold text-ink/40">{giveUp.lapsed_silently_count}</p>
                  <p className="text-[11.5px] text-ink/50">just vanished instead</p>
                </div>
              </div>
            ) : (
              <p className="text-xs text-ink/40">
                Nobody explicitly gave up in this range
                {giveUp && giveUp.lapsed_silently_count > 0 && ` — but ${giveUp.lapsed_silently_count} failed payment(s) were written off after the recovery window passed.`}
              </p>
            )}
            {giveUp && Object.keys(giveUp.by_reason || {}).length > 0 && (
              <p className="text-[11.5px] text-ink/40 mt-3 text-center">
                Mostly after:{" "}
                {Object.entries(giveUp.by_reason)
                  .sort((a, b) => b[1] - a[1])
                  .map(([r, n]) => `${r.replace(/_/g, " ")} (${n})`)
                  .join(", ")}
              </p>
            )}
          </div>

          <h3 className="text-sm font-bold mb-2.5">How the agent is handling it</h3>
          <div className="bg-white border border-ink/10 rounded-lg p-3.5 mb-3">
            <p className="text-xs font-bold mb-1">Does trying again actually work?</p>
            <p className="text-[11.5px] text-ink/40 mb-2.5">
              Currently allowed up to {retry ? retry.max_auto_retries : "—"} tries
              {retry?.avg_attempts != null && ` · customers average ${retry.avg_attempts}`}
            </p>
            {retry && retry.ladder.length > 0 ? (
              retry.ladder.map((step) => (
                <StatRow
                  key={step.attempt}
                  label={`Reached try ${step.attempt} (${step.runs_reaching} purchase${step.runs_reaching === 1 ? "" : "s"})`}
                  value={step.recovery_rate_pct == null ? "—" : `${step.recovery_rate_pct}% eventually worked`}
                  colorClass={step.recovered > 0 ? "text-soft" : "text-failed"}
                />
              ))
            ) : (
              <p className="text-xs text-ink/40">No failed payments in this range.</p>
            )}
          </div>

          <div className="flex gap-3 mb-3 flex-wrap">
            <div className="flex-1 min-w-[280px] bg-white border border-ink/10 rounded-lg p-3.5">
              <p className="text-xs font-bold mb-2">Handed to a human</p>
              <StatRow label="Because the amount was too large" value={esc ? esc.purely_amount_triggered : 0} colorClass="text-declined" />
              <StatRow label="Because of an actual red flag" value={esc ? esc.other_reason : 0} colorClass="text-failed" />
              {esc && esc.near_floor_count > 0 && (
                <p className="text-[11px] text-ink/40 mt-1.5">
                  {esc.near_floor_count} were only just over the {inr(esc.current_threshold_inr)} cutoff.
                </p>
              )}
            </div>
            <div className="flex-1 min-w-[280px] bg-white border border-ink/10 rounded-lg p-3.5">
              <p className="text-xs font-bold mb-2">How much of this was really the AI</p>
              {agent && (
                <>
                  <StatRow label="Decided by the AI" value={agent.llm_decisions} colorClass="text-soft" />
                  <StatRow label="Fell back to the plain rulebook" value={agent.fallback_decisions} colorClass={agent.fallback_decisions > 0 ? "text-declined" : undefined} />
                  <StatRow label="AI's escalation rate" value={agent.llm_escalation_rate_pct == null ? "—" : `${agent.llm_escalation_rate_pct}%`} />
                  <StatRow label="Rulebook's escalation rate" value={agent.rules_fallback_escalation_rate_pct == null ? "—" : `${agent.rules_fallback_escalation_rate_pct}%`} />
                </>
              )}
            </div>
          </div>

          <div className="flex gap-3 mb-6 flex-wrap">
            {["payment_failure", "dropoff"].map(
              (pl) =>
                cov?.[pl] && (
                  <div key={pl} className="flex-1 min-w-[280px] bg-white border border-ink/10 rounded-lg p-3.5">
                    <p className="text-xs font-bold mb-2">
                      {pl === "payment_failure" ? "Failed payments" : "Abandoned checkouts"} — sent to a human for low confidence
                    </p>
                    <StatRow label="Decisions made" value={cov[pl]!.total_decisions} />
                    <StatRow label="Overridden by the confidence gate" value={cov[pl]!.escalated_by_gate} colorClass={cov[pl]!.escalated_by_gate > 0 ? "text-declined" : undefined} />
                    <StatRow label="Rate" value={cov[pl]!.escalated_rate_pct == null ? "—" : `${cov[pl]!.escalated_rate_pct}%`} />
                  </div>
                ),
            )}
            {repeatOff && Object.keys(repeatOff).length > 0 && (
              <div className="flex-1 min-w-[280px] bg-white border border-ink/10 rounded-lg p-3.5">
                <p className="text-xs font-bold mb-2">Customers who failed more than once</p>
                {Object.entries(repeatOff)
                  .sort((a, b) => b[1] - a[1])
                  .map(([tier, n]) => {
                    const c = toneClasses(tierTone(tier));
                    return (
                      <div key={tier} className="flex justify-between py-1 text-[12.5px]">
                        <span className={`inline-flex rounded px-2 py-0.5 text-[11px] font-semibold ${c.badge}`}>{tier}</span>
                        <span className="font-semibold">{n}</span>
                      </div>
                    );
                  })}
              </div>
            )}
          </div>

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

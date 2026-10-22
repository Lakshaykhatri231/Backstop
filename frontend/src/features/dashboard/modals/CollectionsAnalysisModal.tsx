import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import {
  applySuggestionForRecommendation,
  getCollectionsAnalysis,
  type AnalysisRange,
  type AnalysisResult,
  type Recommendation,
} from "@/lib/api/insights";
import { paramLabel } from "@/lib/constants/params";
import { useInvalidateAuditLog } from "@/lib/hooks/useDashboardData";

import { AnalysisModalShell } from "./AnalysisModalShell";
import { RangeSelector } from "./shared/RangeSelector";
import { PatternsList } from "./shared/PatternsList";
import { RecommendationCard } from "./shared/RecommendationCard";

type Evidence = Record<string, { metric: string; n: number; min_sample: number; sufficient: boolean }>;
type Rung = { retry_number: number; runs_reaching: number; recovered_here: number; recovery_rate: number | null };
type Horizon = { n: number; by_horizon: Record<string, { promises: number; kept: number; kept_rate: number | null }>; kept_rate: number | null };
type CallRung = { call_number: number; calls: number; productive: number; no_answer: number; productive_rate: number | null };

type Reason = { reason: string; runs: number; recovered: number; recovery_rate: number | null; median_days_to_recover: number | null; recovered_inr: number; lost_inr: number };
type Outcomes = {
  n: number; counts: Record<string, number>; amount_inr: Record<string, number>;
  still_open_by_days_overdue: Record<string, number>; paid_share: number | null; collected_share_of_amount: number | null;
};
type ChaseStep = { step: number; name: string; sent: number; paid_within_3d: number; paid_rate: number | null };
type LinkGroup = { subject_type: string; links: number; paid: number; partially_paid: number; expired_unpaid: number; still_open: number; paid_rate: number | null; median_hours_to_pay: number | null };
type Source = { source: string; promises: number; kept: number; kept_rate: number | null };
type CallEnds = { n: number; counts: Record<string, number>; shares: Record<string, number | null> };
type Trend = Record<string, { now: number | null; n_now: number; before: number | null; n_before: number }>;

const pct = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x * 100)}%`);
const rupees = (x: number) => `₹${Math.round(x).toLocaleString("en-IN")}`;
const words = (s: string) => s.replace(/_/g, " ");
const LINK_LABEL: Record<string, string> = { invoice: "Invoice links", mandate_run: "Failed-debit links", order: "Failed-order links" };
const CALL_LABEL: Record<string, string> = {
  promise: "ended with a promise", payment_link: "ended with a payment link", callback: "asked for a callback",
  no_answer: "went unanswered", handed_to_person: "handed to a person", other: "other",
};
const TREND_LABEL: Record<string, string> = {
  mandate_recovery_rate: "Failed debits recovered", promise_kept_rate: "Promises kept", voice_productive_rate: "Calls that were productive",
};

function Card({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <div className="bg-white border border-ink/10 rounded-lg p-3.5 mb-3">
      <p className="text-xs font-bold">{title}</p>
      {note && <p className="text-[11.5px] text-ink/40 mb-2">{note}</p>}
      <div className={note ? "" : "mt-2"}>{children}</div>
    </div>
  );
}

function Row({ label, value, bad }: { label: string; value: string | number; bad?: boolean }) {
  return (
    <div className="flex justify-between py-1 text-[12.5px]">
      <span className="text-ink/50">{label}</span>
      <span className={`font-semibold ${bad ? "text-failed" : ""}`}>{value}</span>
    </div>
  );
}

export function CollectionsAnalysisModal({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
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
    try {
      setResult(await getCollectionsAnalysis(range));
    } finally {
      setLoading(false);
    }
  }

  async function implement(rec: Recommendation, i: number) {
    await applyMutation.mutateAsync({ rec, seq: result?.audit_sequence_num });
    setApplied((a) => ({ ...a, [i]: true }));
    invalidateAuditLog();
  }

  const evidence = result?.["evidence"] as Evidence | undefined;
  const attempts = result?.["mandate_attempts"] as { n: number; recovered: number; lost: number; recovery_rate: number | null; ladder: Rung[] } | undefined;
  const timing = result?.["mandate_timing"] as { recovered: number; median_days: number | null; p90_days: number | null; distribution: Record<string, number> } | undefined;
  const horizon = result?.["promises_by_horizon"] as { b2c: Horizon; b2b: Horizon } | undefined;
  const late = result?.["promise_late_payment"] as { n: number; buckets: Record<string, number>; share_paid_within_2d_after: number | null } | undefined;
  const voice = result?.["voice_by_attempt"] as { n: number; ladder: CallRung[] } | undefined;
  const reasons = result?.["mandate_failure_reasons"] as { n: number; by_reason: Reason[] } | undefined;
  const outcomes = result?.["receivables_outcomes"] as Outcomes | undefined;
  const chase = result?.["chase_step_effectiveness"] as { n: number; steps: ChaseStep[] } | undefined;
  const links = result?.["link_conversion"] as { n: number; by_subject: LinkGroup[] } | undefined;
  const sources = result?.["promises_by_source"] as { n: number; by_source: Source[] } | undefined;
  const callEnds = result?.["voice_outcomes"] as CallEnds | undefined;
  const trend = result?.["trend"] as Trend | null | undefined;
  const dropped = (result?.["dropped_for_insufficient_evidence"] as string[] | undefined) ?? [];
  const minSample = result?.["min_sample"] as number | undefined;

  return (
    <AnalysisModalShell open={open} onOpenChange={onOpenChange} title="🧾 Collections Analysis">
      <RangeSelector range={range} onChange={setRange} onAnalyze={analyze} loading={loading} accentClass="bg-indigo/15 text-indigo" />
      <p className="text-[11.5px] text-ink/40 mb-4">
        Failed recurring debits, promises to pay, voice calls, B2B invoices and payment links. A setting is only ever recommended on when at least{" "}
        {minSample ?? 20} resolved cases back it.
      </p>

      {!result && !loading && <p className="text-center text-ink/40 text-sm py-8">Pick a time range and click Analyze.</p>}
      {loading && <p className="text-center text-ink/40 text-sm py-8">Going through retries, promises, calls and invoices...</p>}

      {result && (
        <div>
          {result.summary && <div className="bg-white border border-ink/10 rounded-lg p-3 mb-4 text-sm">{result.summary}</div>}
          {result.llm_error && (
            <div className="bg-declined/10 border border-declined/40 rounded-lg p-3 mb-4 text-xs text-declined">
              Recommendations unavailable ({result.llm_error}). Metrics below are still accurate.
            </div>
          )}

          <PatternsList patterns={result.patterns} />

          <h3 className="text-sm font-bold mb-2">Is there enough data for each setting?</h3>
          <div className="overflow-x-auto mb-5">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="text-ink/40">
                  {["Setting", "Resolved cases", "Needed", ""].map((h) => (
                    <th key={h} className="px-2 py-1.5 border-b border-ink/10 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {evidence && Object.entries(evidence).map(([param, e]) => (
                  <tr key={param} className="border-b border-ink/10">
                    <td className="px-2 py-1.5">{paramLabel(param)}</td>
                    <td className="px-2 py-1.5 tabular-nums">{e.n}</td>
                    <td className="px-2 py-1.5 tabular-nums">{e.min_sample}</td>
                    <td className={`px-2 py-1.5 font-semibold ${e.sufficient ? "text-soft" : "text-ink/40"}`}>
                      {e.sufficient ? "enough" : "not yet"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <Card title="Failed recurring debits" note={attempts && attempts.n > 0
            ? `${attempts.n} settled runs · ${pct(attempts.recovery_rate)} recovered`
            : "No settled runs in this range."}>
            {attempts?.ladder.map((r) => (
              <Row key={r.retry_number}
                   label={`Retry ${r.retry_number} (${r.runs_reaching} run${r.runs_reaching === 1 ? "" : "s"} reached it)`}
                   value={`${r.recovered_here} recovered · ${pct(r.recovery_rate)}`}
                   bad={r.runs_reaching > 0 && r.recovered_here === 0} />
            ))}
            {timing && timing.recovered > 0 && (
              <p className="text-[11.5px] text-ink/40 mt-2">
                Recoveries took a median of {timing.median_days} days (90% within {timing.p90_days}).
              </p>
            )}
          </Card>

          <Card title="Promises kept, by how far ahead they were made">
            {horizon && (["b2c", "b2b"] as const).map((seg) => (
              <div key={seg} className="mb-2">
                <p className="text-[11.5px] font-semibold text-ink/60">
                  {seg === "b2c" ? "Consumers" : "Businesses"} · {horizon[seg].n} resolved · {pct(horizon[seg].kept_rate)} kept
                </p>
                {Object.entries(horizon[seg].by_horizon).filter(([, b]) => b.promises > 0).map(([lbl, b]) => (
                  <Row key={lbl} label={`${lbl} out (${b.promises})`} value={pct(b.kept_rate)} />
                ))}
              </div>
            ))}
            {late && late.n > 0 && (
              <p className="text-[11.5px] text-ink/40">
                Of {late.n} broken promises, {pct(late.share_paid_within_2d_after)} were paid within 2 days of the date.
              </p>
            )}
          </Card>

          <Card title="Voice calls" note={voice && voice.n > 0 ? `${voice.n} calls placed` : "No calls placed in this range."}>
            {voice?.ladder.map((c) => (
              <Row key={c.call_number} label={`Call ${c.call_number} (${c.calls})`}
                   value={`${pct(c.productive_rate)} led to a payment link or promise`} />
            ))}
          </Card>

          <h3 className="text-sm font-bold mb-2 text-ink/60">Supporting detail</h3>
          <p className="text-[11.5px] text-ink/40 mb-2">
            These explain why the numbers above look as they do. They back the observations and the model's reasoning, but a setting is
            only recommended on when its own row above has enough cases.
          </p>

          {trend && (
            <Card title="Is it getting better?" note="This range against the equal range just before it. Only settled cases count, so read small changes with care.">
              {Object.entries(trend).map(([k, t]) => (
                <Row key={k} label={`${TREND_LABEL[k] ?? words(k)} (${t.n_now} vs ${t.n_before} cases)`}
                     value={`${pct(t.now)} now · ${pct(t.before)} before`}
                     bad={t.now != null && t.before != null && t.now < t.before - 0.1} />
              ))}
            </Card>
          )}

          <Card title="Why recurring debits fail, and whether retrying helps"
                note={reasons && reasons.n > 0 ? `${reasons.n} settled runs` : "No settled runs in this range."}>
            {reasons?.by_reason.map((g) => (
              <Row key={g.reason} label={`${words(g.reason)} (${g.runs})`}
                   value={`${pct(g.recovery_rate)} recovered${g.median_days_to_recover != null ? ` · median ${g.median_days_to_recover} d` : ""} · ${rupees(g.lost_inr)} lost`}
                   bad={g.runs >= 5 && (g.recovery_rate ?? 0) < 0.2} />
            ))}
          </Card>

          <Card title="What became of every overdue invoice"
                note={outcomes && outcomes.n > 0 ? `${outcomes.n} invoices went overdue in this range, including the ones never paid.` : "No overdue invoices in this range."}>
            {outcomes && outcomes.n > 0 && (
              <>
                {(["paid", "partially_paid", "still_open", "disputed", "written_off"] as const).map((k) => (
                  <Row key={k} label={`${words(k)} (${outcomes.counts[k] ?? 0})`} value={rupees(outcomes.amount_inr[k] ?? 0)}
                       bad={(k === "written_off" || k === "disputed") && (outcomes.counts[k] ?? 0) > 0} />
                ))}
                <p className="text-[11.5px] text-ink/40 mt-2">
                  Still open by days overdue: {Object.entries(outcomes.still_open_by_days_overdue).map(([k, v]) => `${k} ${v}`).join(" · ")}.
                  {" "}{pct(outcomes.paid_share)} of invoices and {pct(outcomes.collected_share_of_amount)} of the amount were collected.
                </p>
              </>
            )}
          </Card>

          <Card title="Which reminder is followed by payment"
                note="Counted when payment came within 3 days of the last reminder before it (the last one gets the credit, so an early step that only prepares the ground looks weaker than it is).">
            {chase && chase.steps.length === 0 && <p className="text-xs text-ink/40">No reminders ran in this range.</p>}
            {chase?.steps.map((c) => (
              <Row key={c.step} label={`${c.name} (${c.sent} sent)`} value={`${pct(c.paid_rate)} followed by payment`} />
            ))}
          </Card>

          <Card title="Payment links" note="Real links only (stand-ins can't be paid). A link replaced by a newer one is not counted.">
            {links && links.by_subject.length === 0 && <p className="text-xs text-ink/40">No real payment links in this range.</p>}
            {links?.by_subject.map((g) => (
              <Row key={g.subject_type} label={`${LINK_LABEL[g.subject_type] ?? words(g.subject_type)} (${g.links})`}
                   value={`${pct(g.paid_rate)} paid · ${g.expired_unpaid} expired unpaid${g.median_hours_to_pay != null ? ` · median ${g.median_hours_to_pay} h to pay` : ""}`} />
            ))}
          </Card>

          <Card title="Promises, by where they came from">
            {sources && sources.by_source.length === 0 && <p className="text-xs text-ink/40">No resolved promises in this range.</p>}
            {sources?.by_source.map((g) => (
              <Row key={g.source} label={`${words(g.source)} (${g.promises})`} value={`${pct(g.kept_rate)} kept`} />
            ))}
          </Card>

          <Card title="How calls end" note={callEnds && callEnds.n > 0 ? `${callEnds.n} calls placed` : "No calls placed in this range."}>
            {callEnds && callEnds.n > 0 && Object.entries(callEnds.counts).filter(([, v]) => v > 0).map(([k, v]) => (
              <Row key={k} label={CALL_LABEL[k] ?? words(k)} value={`${v} · ${pct(callEnds.shares[k])}`} bad={k === "handed_to_person" && (callEnds.shares[k] ?? 0) >= 0.3} />
            ))}
          </Card>

          <h3 className="text-sm font-bold mb-2 text-ink/60">Recommendations</h3>
          {dropped.length > 0 && (
            <p className="text-[11.5px] text-ink/40 mb-2">
              Not recommended for lack of data: {dropped.map(paramLabel).join(", ")}.
            </p>
          )}
          {result.recommendations.length === 0 && !result.llm_error && (
            <p className="text-xs text-ink/40">No changes recommended from this data.</p>
          )}
          {result.recommendations.map((rec, i) => (
            <RecommendationCard key={i} rec={rec} applied={!!applied[i]} applying={applyMutation.isPending}
                                onImplement={() => implement(rec, i)} />
          ))}
        </div>
      )}
    </AnalysisModalShell>
  );
}

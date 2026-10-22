import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useInvoices, usePromises, useReceivablesSummary, useVoiceCalls } from "@/lib/hooks/useDashboardData";
import type { Tone } from "@/lib/constants/tone";

import { StatCard } from "./components/StatCard";
import { ToneBadge } from "./components/ToneBadge";
import { InvoiceImport } from "./components/InvoiceImport";
import { ContactsPanel } from "./components/ContactsPanel";

function fmtInr(n: number) {
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

const INVOICE_TONE: Record<string, Tone> = {
  open: "neutral",
  overdue: "declined",
  partially_paid: "loyal",
  paid: "soft",
  disputed: "failed",
  written_off: "neutral",
};

const PROMISE_TONE: Record<string, Tone> = {
  open: "loyal",
  kept: "soft",
  partially_kept: "indigo",
  broken: "failed",
  cancelled: "neutral",
};

// Where a promise stands NOW (the stored status only says open/kept/broken).
const PROMISE_STATE: Record<string, { label: string; tone: Tone }> = {
  waiting: { label: "Waiting", tone: "loyal" },
  due_today: { label: "Due today", tone: "declined" },
  in_grace: { label: "Date passed - not judged yet", tone: "declined" },
  kept: { label: "Kept", tone: "soft" },
  partly_kept: { label: "Partly kept", tone: "indigo" },
  broken: { label: "Broken", tone: "failed" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

const INTENT_TONE: Record<string, Tone> = {
  PAY_NOW: "soft",
  PROMISE: "indigo",
  DISPUTE: "failed",
  WRONG_NUMBER: "neutral",
  CALLBACK: "loyal",
  REFUSED: "declined",
  UNCLEAR: "neutral",
};

const card =
  "rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-4";

function Empty({ children }: { children: string }) {
  return <div className="text-center py-8 text-ink/40 text-sm">{children}</div>;
}

export function ReceivablesTab() {
  const summaryQ = useReceivablesSummary();
  const invoicesQ = useInvoices();
  const promisesQ = usePromises();
  const callsQ = useVoiceCalls();

  if (!summaryQ.data || !invoicesQ.data || !promisesQ.data || !callsQ.data) {
    return <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>;
  }

  const s = summaryQ.data;
  const maxAging = Math.max(1, ...Object.values(s.aging).map((b) => b.amount_inr));

  return (
    <div>
      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-1">B2B Receivables</h2>
        <p className="text-xs text-ink/50 mb-4">
          Kept separate from consumer recovery: these figures never enter At Risk or the recovery rate.
        </p>
        <div className="flex flex-wrap justify-around gap-4">
          <StatCard label="Overdue" value={fmtInr(s.overdue_inr)} tone="declined" />
          <StatCard label="Collected" value={fmtInr(s.collected_inr)} tone="soft" />
          <StatCard label="Written off" value={fmtInr(s.written_off_inr)} tone="failed" />
          <StatCard label="Not yet due" value={fmtInr(s.not_yet_due_inr)} />
          <StatCard
            label="Avg days to collect"
            value={s.avg_days_to_collect === null ? "—" : s.avg_days_to_collect}
          />
          <StatCard label="Disputed" value={s.disputed} tone={s.disputed > 0 ? "failed" : "default"} />
        </div>
      </div>

      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-3">Aging (days past due)</h2>
        <div className="space-y-2">
          {Object.entries(s.aging).map(([bucket, b]) => (
            <div key={bucket} className="flex items-center gap-3 text-sm">
              <span className="w-16 text-ink/50 text-xs">{bucket}</span>
              <div className="flex-1 h-5 rounded bg-ink/5 overflow-hidden">
                <div
                  className="h-full bg-declined/70"
                  style={{ width: `${(b.amount_inr / maxAging) * 100}%` }}
                />
              </div>
              <span className="w-28 text-right tabular-nums text-xs">{fmtInr(b.amount_inr)}</span>
              <span className="w-16 text-right text-xs text-ink/40">{b.invoices} inv</span>
            </div>
          ))}
        </div>
      </div>

      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-1">Import invoices from CSV</h2>
        <p className="text-xs text-ink/50 mb-3">Preview first, then confirm. Each invoice gets a payment link and a chase ladder.</p>
        <InvoiceImport />
      </div>

      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-3">Invoices</h2>
        {invoicesQ.data.length === 0 ? (
          <Empty>No invoices yet. Create an account and invoice via POST /receivables/*.</Empty>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Invoice</TableHead>
                <TableHead>Client</TableHead>
                <TableHead>Outstanding</TableHead>
                <TableHead>Days past due</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Customer's page</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {invoicesQ.data.map((i) => (
                <TableRow key={i.id}>
                  <TableCell className="text-xs font-medium">{i.invoice_no}</TableCell>
                  <TableCell className="text-xs">{i.company_name ?? "—"}</TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {fmtInr(i.outstanding_inr)}
                    {i.amount_paid > 0 && (
                      <span className="text-ink/40"> of {fmtInr(i.amount_inr)}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{i.days_past_due || "—"}</TableCell>
                  <TableCell>
                    <ToneBadge label={i.status.replace("_", " ")} tone={INVOICE_TONE[i.status] ?? "neutral"} />
                    {i.escalated_at && <span className="ml-1"><ToneBadge label="escalated" tone="declined" /></span>}
                  </TableCell>
                  <TableCell>
                    <button
                      type="button"
                      className="text-xs underline text-ink/60"
                      onClick={() => navigator.clipboard?.writeText(`${window.location.origin}${i.self_serve_path}`)}
                    >
                      Copy link
                    </button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-1">Promises to pay</h2>
        <p className="text-xs text-ink/50 mb-3">
          A promise pauses chasing and moves no money; only a real payment (or a final give-up) does.
        </p>
        {Object.keys(promisesQ.data.kept_rate_by_source).length > 0 && (
          <div className="flex flex-wrap gap-4 mb-3 text-xs text-ink/60">
            {Object.entries(promisesQ.data.kept_rate_by_source).map(([src, r]) => (
              <span key={src}>
                {src.replace("_", " ")}: kept {Math.round(r.kept_rate * 100)}% ({r.kept}/{r.resolved})
              </span>
            ))}
          </div>
        )}
        {promisesQ.data.promises.length === 0 ? (
          <Empty>No promises yet.</Empty>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Against</TableHead>
                <TableHead>Amount</TableHead>
                <TableHead>Promised for</TableHead>
                <TableHead>Captured via</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>What it means</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {promisesQ.data.promises.map((p) => (
                <TableRow key={p.id}>
                  <TableCell className="text-xs">{p.subject_type.replace("_", " ")}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtInr(p.amount_inr)}</TableCell>
                  <TableCell className="text-xs">{p.promised_date}</TableCell>
                  <TableCell className="text-xs">{p.captured_via.replace("_", " ")}</TableCell>
                  <TableCell>
                    <ToneBadge
                      label={(PROMISE_STATE[p.state] ?? { label: p.status.replace("_", " ") }).label}
                      tone={(PROMISE_STATE[p.state] ?? { tone: PROMISE_TONE[p.status] ?? "neutral" }).tone}
                    />
                  </TableCell>
                  <TableCell className="text-xs text-ink/60 max-w-[360px]">{p.label}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-3">Contacts &amp; recurring debits</h2>
        <ContactsPanel />
      </div>

      <div className={card}>
        <h2 className="font-display font-semibold text-lg mb-1">Voice calls</h2>
        <p className="text-xs text-ink/50 mb-3">
          Simulated provider in v1. A call needs a verified number and the customer's own consent, runs 08:00–19:00 IST
          only, and never reports recovery.
        </p>
        {callsQ.data.length === 0 ? (
          <Empty>No calls yet.</Empty>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>About</TableHead>
                <TableHead>Language</TableHead>
                <TableHead>Transcript</TableHead>
                <TableHead>Intent</TableHead>
                <TableHead>Routed to</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {callsQ.data.map((c) => (
                <TableRow key={c.id}>
                  <TableCell className="text-xs">{c.subject_type.replace("_", " ")}</TableCell>
                  <TableCell className="text-xs">{c.language}</TableCell>
                  <TableCell className="text-xs max-w-[360px]">
                    {c.transcript ? c.transcript : <span className="text-ink/40">no answer</span>}
                  </TableCell>
                  <TableCell>
                    {c.intent ? (
                      <ToneBadge label={c.intent} tone={INTENT_TONE[c.intent] ?? "neutral"} />
                    ) : (
                      "—"
                    )}
                    {c.intent_source === "keyword_fallback" && (
                      <span className="ml-1 text-[10px] text-ink/40">fallback</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">{c.routed_to ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}

import { Fragment, useState } from "react";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { auditTypeTone } from "@/lib/constants/audit";
import type { AuditLogEntry } from "@/lib/api/outcomes";
import { ToneBadge } from "./ToneBadge";

// `details` comes over the wire as a JSON-encoded string, not a parsed
// object — parse it defensively and fall back to the raw text if it's ever
// not valid JSON (matches the old dashboard's AuditDetailPanel behavior).
function parseDetails(details: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(details);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function summarize(details: string): string {
  const parsed = parseDetails(details);
  if (!parsed) return details;
  return Object.entries(parsed)
    .slice(0, 2)
    .map(([k, v]) => `${k}: ${typeof v === "object" && v !== null ? "{...}" : String(v)}`)
    .join(" · ");
}

function DetailPanel({ details }: { details: string }) {
  const parsed = parseDetails(details);
  if (!parsed) {
    return <pre className="text-[11px] text-ink/60 whitespace-pre-wrap">{details}</pre>;
  }
  return (
    <div className="flex flex-col gap-2">
      {Object.entries(parsed).map(([key, value]) => {
        const isSimple = value === null || typeof value !== "object";
        return (
          <div key={key}>
            <div className="text-[10.5px] text-ink/40 uppercase tracking-wide">{key.replace(/_/g, " ")}</div>
            {isSimple ? (
              <div className="text-xs text-ink">{String(value)}</div>
            ) : (
              <pre className="text-[11px] text-ink/60 bg-cream border border-ink/10 rounded-md p-2 overflow-x-auto leading-relaxed">
                {JSON.stringify(value, null, 2)}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function AuditLogTable({ log }: { log: AuditLogEntry[] }) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const toggle = (seq: number) => setExpanded((prev) => ({ ...prev, [seq]: !prev[seq] }));

  if (log.length === 0) {
    return <div className="text-center py-10 text-ink/40 text-sm">No audit entries yet.</div>;
  }

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-6" />
            <TableHead>#</TableHead>
            <TableHead>Action Type</TableHead>
            <TableHead>Details</TableHead>
            <TableHead>Prev Hash</TableHead>
            <TableHead>Entry Hash</TableHead>
            <TableHead>Time</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {log.map((e) => {
            const isOpen = !!expanded[e.sequence_num];
            return (
              <Fragment key={e.sequence_num}>
                <TableRow>
                  <TableCell>
                    <button onClick={() => toggle(e.sequence_num)} className="text-ink/40 text-xs">
                      {isOpen ? "▾" : "▸"}
                    </button>
                  </TableCell>
                  <TableCell className="text-xs text-ink/40 tabular-nums">{e.sequence_num}</TableCell>
                  <TableCell>
                    <ToneBadge label={e.action_type.replace(/_/g, " ")} tone={auditTypeTone(e.action_type)} />
                  </TableCell>
                  <TableCell className="text-xs text-ink/50 max-w-[240px] truncate">{summarize(e.details)}</TableCell>
                  <TableCell className="text-[10px] font-mono text-ink/40">{e.prev_hash}</TableCell>
                  <TableCell className="text-[10px] font-mono text-ink/40">{e.entry_hash}</TableCell>
                  <TableCell className="text-xs text-ink/40 whitespace-nowrap">
                    {new Date(e.created_at).toLocaleTimeString()}
                  </TableCell>
                </TableRow>
                {isOpen && (
                  <TableRow>
                    <TableCell />
                    <TableCell colSpan={6} className="bg-cream/60">
                      <DetailPanel details={e.details} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

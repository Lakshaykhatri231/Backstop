import { Fragment, useState } from "react";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { actionLabel, actionTone } from "@/lib/constants/actions";
import { eventTypeBadge, type FeedItem } from "@/lib/constants/event-feed";
import { outcomeTone } from "@/lib/constants/outcomes";
import { sourceLabel, sourceTone } from "@/lib/constants/sources";
import { ToneBadge } from "./ToneBadge";

const COLUMNS = ["Type", "Customer", "Amount", "Signal", "Action", "Source", "Outcome"];

function fmtAmt(n: number | null | undefined) {
  return `₹${Math.round(n || 0)}`;
}

export function EventFeedTable({ feed }: { feed: FeedItem[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (feed.length === 0) {
    return <div className="text-center py-10 text-ink/40 text-sm">No activity yet — use the Store page to generate some.</div>;
  }

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            {COLUMNS.map((h) => (
              <TableHead key={h} className="whitespace-nowrap text-xs">
                {h}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {feed.map((item, i) => {
            const isExp = expanded === i;
            const toggle = () => setExpanded(isExp ? null : i);

            if (item.kind === "cart") {
              const ce = item.cart;
              const isCancel = ce.event_type === "explicit_cancel";
              return (
                <Fragment key={i}>
                  <TableRow onClick={toggle} className="cursor-pointer">
                    <TableCell>
                      <ToneBadge label={isCancel ? "Cart Cancel" : "Cart Abandon"} tone={isCancel ? "failed" : "declined"} />
                    </TableCell>
                    <TableCell className="text-xs text-ink/60">{ce.customer_name || "—"}</TableCell>
                    <TableCell className="text-xs tabular-nums">{fmtAmt(ce.amount_inr)}</TableCell>
                    <TableCell className="text-xs text-ink/60">
                      {(isCancel ? "cancelled before checkout" : "idle cart timeout") + (ce.status ? ` · ${ce.status}` : "")}
                    </TableCell>
                    <TableCell>
                      <ToneBadge label={actionLabel(ce.action)} tone={actionTone(ce.action)} />
                    </TableCell>
                    <TableCell>
                      <ToneBadge label="Rules" tone="loyal" />
                    </TableCell>
                    <TableCell>
                      <ToneBadge label={ce.outcome || "—"} tone={outcomeTone(ce.outcome)} />
                    </TableCell>
                  </TableRow>
                  {isExp && ce.reasoning && (
                    <TableRow>
                      <TableCell colSpan={7} className="bg-cream/60 text-xs">
                        <span className="text-indigo font-medium">Reasoning: </span>
                        <span className="italic text-ink/60">{ce.reasoning}</span>
                        <div className="text-ink/40 mt-1.5">
                          Confidence: {Math.round((ce.confidence || 0) * 100)}%
                          {ce.incentive_pct != null && (
                            <span className="text-soft ml-2.5">
                              Incentive: {ce.incentive_pct}% → ₹{(ce.final_amount_inr || 0).toFixed(2)}
                            </span>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              );
            }

            if (item.kind === "payment") {
              const o = item.order;
              return (
                <TableRow key={i}>
                  <TableCell>
                    <ToneBadge label="Payment" tone="soft" />
                  </TableCell>
                  <TableCell className="text-xs text-ink/60">{o.customer_name || "—"}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtAmt(o.amount_inr)}</TableCell>
                  <TableCell className="text-xs text-ink/60">
                    {o.recovered_from_cart_event_id ? "recovered via nudge" : "successful payment"}
                  </TableCell>
                  <TableCell className="text-xs text-ink/30">—</TableCell>
                  <TableCell className="text-xs text-ink/30">—</TableCell>
                  <TableCell>
                    <ToneBadge label="captured" tone="soft" />
                  </TableCell>
                </TableRow>
              );
            }

            const e = item.event;
            const isDropoff = e.event_type === "checkout.abandoned" || e.event_type === "checkout_abandoned";
            const typeBadge = eventTypeBadge(e.event_type);
            return (
              <Fragment key={i}>
                <TableRow onClick={toggle} className="cursor-pointer">
                  <TableCell>
                    <ToneBadge label={typeBadge.label} tone={typeBadge.tone} />
                  </TableCell>
                  <TableCell className="text-xs text-ink/60">{e.customer_name || e.customer_id || "—"}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtAmt(e.amount_inr)}</TableCell>
                  <TableCell className="text-xs text-ink/60 max-w-[150px] truncate">
                    {isDropoff ? "Abandonment" : (e.failure_reason || "—").replace(/_/g, " ")}
                  </TableCell>
                  <TableCell>
                    {e.decision && <ToneBadge label={actionLabel(e.decision.action)} tone={actionTone(e.decision.action)} />}
                  </TableCell>
                  <TableCell>
                    <ToneBadge label={sourceLabel(e.decision?.source)} tone={sourceTone(e.decision?.source)} />
                  </TableCell>
                  <TableCell>
                    <ToneBadge label={e.decision?.outcome || "—"} tone={outcomeTone(e.decision?.outcome)} />
                  </TableCell>
                </TableRow>
                {isExp && e.decision && (
                  <TableRow>
                    <TableCell colSpan={7} className="bg-cream/60 text-xs">
                      <span className="text-indigo font-medium">Reasoning: </span>
                      <span className="italic text-ink/60">{e.decision.reasoning}</span>
                      <div className="text-ink/40 mt-1.5">
                        Confidence: {Math.round((e.decision.confidence || 0) * 100)}%
                        {e.decision.escalated && <span className="text-declined ml-2.5">⚠ Confidence Gate Override</span>}
                      </div>
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

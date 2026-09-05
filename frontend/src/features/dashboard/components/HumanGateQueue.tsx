import { useState } from "react";

import type { OutcomeEvent } from "@/lib/api/outcomes";
import type { MerchantCartEvent } from "@/lib/api/merchant";

type QueueItem = {
  id: string;
  tag: string;
  tone: "declined" | "failed" | "indigo";
  title: string;
  meta: string;
  reasoning: string | null;
};

function fromEvents(events: OutcomeEvent[]): QueueItem[] {
  return events
    .filter((e) => e.decision?.escalated)
    .map((e) => ({
      id: e.event_id,
      tag: "F",
      tone: "failed" as const,
      title: `${e.customer_name || e.customer_id || "Unknown"} — ${(e.failure_reason || e.event_type).replace(/_/g, " ")}`,
      meta: `Confidence ${e.decision?.confidence?.toFixed(2) ?? "—"} · ₹${Math.round(e.amount_inr)}`,
      reasoning: e.decision?.reasoning ?? null,
    }));
}

function fromCartEvents(cartEvents: MerchantCartEvent[]): QueueItem[] {
  return cartEvents
    .filter((c) => c.action === "escalate_to_human")
    .map((c) => ({
      id: c.id,
      tag: c.event_type === "explicit_cancel" ? "D" : "S",
      tone: c.event_type === "explicit_cancel" ? ("declined" as const) : ("indigo" as const),
      title: `${c.customer_name || c.customer_id} — ${c.tier_at_time}, ${c.event_type.replace(/_/g, " ")}`,
      meta: `Confidence ${c.confidence?.toFixed(2) ?? "—"} · ₹${Math.round(c.amount_inr)}`,
      reasoning: c.reasoning,
    }));
}

// Real data, not the mockup's hardcoded fake queue — every /merchant/*
// and /outcomes/* endpoint is open and read-only, so "Review" here expands
// the agent's own reasoning rather than performing an action the backend
// has no endpoint for.
export function HumanGateQueue({ events, cartEvents }: { events: OutcomeEvent[]; cartEvents: MerchantCartEvent[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const items = [...fromEvents(events), ...fromCartEvents(cartEvents)].slice(0, 6);

  if (items.length === 0) {
    return <p className="text-sm text-ink/40">No cases waiting on a human right now.</p>;
  }

  return (
    <div className="space-y-3">
      {items.map((q) => (
        <div key={q.id}>
          <div className="flex items-center gap-3">
            <span
              className={`size-9 shrink-0 rounded-lg grid place-items-center text-xs font-bold ${
                q.tone === "failed" ? "bg-failed/15 text-failed" : q.tone === "declined" ? "bg-declined/15 text-declined" : "bg-indigo/15 text-indigo"
              }`}
            >
              {q.tag}
            </span>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium truncate">{q.title}</p>
              <p className="text-xs text-ink/50 tabular-nums">{q.meta}</p>
            </div>
            <button
              onClick={() => setExpanded(expanded === q.id ? null : q.id)}
              className="text-xs font-semibold px-2.5 py-1.5 rounded-md bg-ink/5 shrink-0"
            >
              {expanded === q.id ? "Hide" : "Review"}
            </button>
          </div>
          {expanded === q.id && q.reasoning && (
            <p className="text-xs text-ink/50 italic mt-1.5 ml-12">{q.reasoning}</p>
          )}
        </div>
      ))}
    </div>
  );
}

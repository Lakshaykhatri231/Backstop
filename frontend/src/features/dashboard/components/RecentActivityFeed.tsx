import { actionLabel, actionTone } from "@/lib/constants/actions";
import { eventTypeBadge, type FeedItem } from "@/lib/constants/event-feed";
import { outcomeTone } from "@/lib/constants/outcomes";
import { ToneBadge } from "./ToneBadge";

function fmtAmt(n: number | null | undefined) {
  return `₹${Math.round(n || 0)}`;
}

export function RecentActivityFeed({ feed }: { feed: FeedItem[] }) {
  const recent = feed.slice(0, 6);

  if (recent.length === 0) {
    return (
      <div className="text-center py-8 text-ink/40 text-sm">
        No activity yet — run <code className="bg-ink/5 px-1 rounded">python scripts/seed_demo_data.py</code> or use
        the Store page.
      </div>
    );
  }

  return (
    <div>
      {recent.map((item, i) => {
        const isLast = i === recent.length - 1;
        const row = "flex items-center gap-3 py-2" + (isLast ? "" : " border-b border-ink/10");

        if (item.kind === "cart") {
          const ce = item.cart;
          const isCancel = ce.event_type === "explicit_cancel";
          return (
            <div key={i} className={row}>
              <ToneBadge label={isCancel ? "Cart Cancel" : "Cart Abandon"} tone={isCancel ? "failed" : "declined"} />
              <div className="flex-1 text-xs text-ink/60">
                {ce.customer_name || "—"} <span className="text-ink/40 ml-2">{fmtAmt(ce.amount_inr)}</span>
              </div>
              <ToneBadge label={actionLabel(ce.action)} tone={actionTone(ce.action)} />
              <ToneBadge label={ce.status || ce.outcome || "—"} tone={outcomeTone(ce.outcome)} />
            </div>
          );
        }

        if (item.kind === "payment") {
          const o = item.order;
          return (
            <div key={i} className={row}>
              <ToneBadge label="Payment" tone="soft" />
              <div className="flex-1 text-xs text-ink/60">
                {o.customer_name || "—"} <span className="text-ink/40 ml-2">{fmtAmt(o.amount_inr)}</span>
              </div>
              {o.recovered_from_cart_event_id && <ToneBadge label="Recovered via nudge" tone="indigo" />}
              <ToneBadge label="captured" tone="soft" />
            </div>
          );
        }

        const e = item.event;
        const typeBadge = eventTypeBadge(e.event_type);
        return (
          <div key={i} className={row}>
            <ToneBadge label={typeBadge.label} tone={typeBadge.tone} />
            <div className="flex-1 text-xs text-ink/60">
              {e.customer_name || e.customer_id || "—"} <span className="text-ink/40 ml-2">{fmtAmt(e.amount_inr)}</span>
            </div>
            {e.decision && <ToneBadge label={actionLabel(e.decision.action)} tone={actionTone(e.decision.action)} />}
            <ToneBadge label={e.decision?.outcome || "—"} tone={outcomeTone(e.decision?.outcome)} />
          </div>
        );
      })}
    </div>
  );
}

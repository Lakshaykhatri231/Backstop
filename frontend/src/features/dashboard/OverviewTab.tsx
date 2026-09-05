import { mergeFeed } from "@/lib/constants/event-feed";
import { useCartEvents, useEvents, useOrders, useOutcomes, useRevenue } from "@/lib/hooks/useDashboardData";

import { StatCard } from "./components/StatCard";
import { DecisionOutcomesChart } from "./components/DecisionOutcomesChart";
import { RecentActivityFeed } from "./components/RecentActivityFeed";
import { HumanGateQueue } from "./components/HumanGateQueue";
import { InsightLayerCard } from "./components/InsightLayerCard";

function fmtInr(n: number | null | undefined) {
  return `₹${Math.round(n || 0).toLocaleString("en-IN")}`;
}

export function OverviewTab({ onOpenAuditLog }: { onOpenAuditLog: () => void }) {
  const revenueQ = useRevenue();
  const outcomesQ = useOutcomes();
  const eventsQ = useEvents(50);
  const ordersQ = useOrders();
  const cartEventsQ = useCartEvents();

  if (!revenueQ.data || !outcomesQ.data || !eventsQ.data || !ordersQ.data || !cartEventsQ.data) {
    return <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>;
  }

  const rev = revenueQ.data;
  const outcomes = outcomesQ.data;
  const events = eventsQ.data;
  const orders = ordersQ.data;
  const cartEvents = cartEventsQ.data;

  const feed = mergeFeed(events, orders, cartEvents);
  const captured = orders.filter((o) => o.status === "captured");
  const recoveryDenominator = rev.total_recovered + rev.total_lost;
  const recoveryPct = recoveryDenominator > 0 ? Math.round((rev.total_recovered / recoveryDenominator) * 100) : null;
  const dropoffs = events.filter((e) => e.event_type === "checkout.abandoned" || e.event_type === "checkout_abandoned").length;
  const failures = events.length - dropoffs;

  return (
    <div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
        <StatCard label="Total Revenue" value={fmtInr(rev.total_revenue)} tone="soft" />
        <StatCard label="Recovered" value={fmtInr(rev.total_recovered)} tone="indigo" />
        <StatCard label="Lost" value={fmtInr(rev.total_lost)} tone="failed" />
        <StatCard
          label="Recovery Rate"
          value={recoveryPct === null ? "—" : `${recoveryPct}%`}
          tone={recoveryPct === null ? "default" : recoveryPct > 30 ? "soft" : "declined"}
        />
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
        <StatCard label="At Risk" value={fmtInr(rev.total_at_risk)} tone="declined" />
        <StatCard label="Payments Captured" value={captured.length} tone="soft" />
        <StatCard label="Recovery Events" value={outcomes.total_events || 0} />
        <StatCard label="Escalated" value={outcomes.escalated || 0} tone="declined" />
      </div>

      <div className="grid grid-cols-3 gap-4 mb-6">
        <StatCard label="Retries Scheduled" value={outcomes.retries || 0} tone="loyal" />
        <StatCard label="Gate Overrides" value={outcomes.escalated_by_confidence_gate || 0} tone="indigo" />
        <StatCard label="Failures / Drop-offs" value={`${failures} / ${dropoffs}`} />
      </div>

      <div className="grid lg:grid-cols-3 gap-4 mb-6">
        <div className="lg:col-span-2 rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
          <h2 className="font-display font-semibold text-lg mb-3">Decision Outcomes (recovery pipeline)</h2>
          <DecisionOutcomesChart outcomes={outcomes} />
        </div>
        <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
          <h2 className="font-display font-semibold text-lg mb-4">Human gate queue</h2>
          <HumanGateQueue events={events} cartEvents={cartEvents} />
        </div>
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          <InsightLayerCard onOpenAuditLog={onOpenAuditLog} />
        </div>
        <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
          <h2 className="font-display font-semibold text-lg mb-3">Recent Activity</h2>
          <RecentActivityFeed feed={feed} />
        </div>
      </div>
    </div>
  );
}

import { useCartEvents, useCustomers, useRevenue } from "@/lib/hooks/useDashboardData";

import { StatCard } from "./components/StatCard";
import { CustomersTable } from "./components/CustomersTable";
import { CartEventsTable } from "./components/CartEventsTable";

function fmtInr(n: number) {
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

export function RevenueCustomersTab() {
  const revenueQ = useRevenue();
  const customersQ = useCustomers();
  const cartEventsQ = useCartEvents();

  if (!revenueQ.data || !customersQ.data || !cartEventsQ.data) {
    return <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>;
  }

  const revenue = revenueQ.data;
  const recoveryDenominator = revenue.total_recovered + revenue.total_lost;
  const recoveryPct = recoveryDenominator > 0 ? Math.round((revenue.total_recovered / recoveryDenominator) * 100) : null;

  return (
    <div>
      <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-4">
        <div className="flex flex-wrap justify-around gap-4">
          <StatCard label="Total Revenue" value={fmtInr(revenue.total_revenue)} tone="soft" />
          <StatCard label="At Risk (real recovery targets)" value={fmtInr(revenue.at_risk_soft)} tone="declined" />
          <StatCard label="At Risk (declined carts)" value={fmtInr(revenue.at_risk_declined)} />
          <StatCard label="At Risk (failed payments)" value={fmtInr(revenue.at_risk_failed)} tone="failed" />
          <StatCard label="Recovered" value={fmtInr(revenue.total_recovered)} tone="indigo" />
          <StatCard label="Lost" value={fmtInr(revenue.total_lost)} tone="failed" />
          {recoveryPct !== null && <StatCard label="Recovery Rate" value={`${recoveryPct}%`} />}
        </div>
      </div>

      <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-4">
        <h2 className="font-display font-semibold text-lg mb-3">Customers &amp; Tiers</h2>
        <CustomersTable customers={customersQ.data} />
      </div>

      <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
        <h2 className="font-display font-semibold text-lg mb-3">Pre-checkout Cart Events</h2>
        <CartEventsTable cartEvents={cartEventsQ.data} />
      </div>
    </div>
  );
}

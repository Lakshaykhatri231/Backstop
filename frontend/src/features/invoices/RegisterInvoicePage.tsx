import { Link } from "@tanstack/react-router";
import { AuthNav, RegisterInvoiceButton } from "@/features/auth/AuthNav";
import { useQuery } from "@tanstack/react-query";

import { MerchantKeyGate } from "@/features/dashboard/MerchantKeyGate";
import { RegisterInvoice } from "@/features/dashboard/components/RegisterInvoice";
import { BusinessCustomers } from "./BusinessCustomers";
import { getInvoices } from "@/lib/api/receivables";

const inr = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;
const day = (iso: string) =>
  new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleDateString("en-IN", { dateStyle: "medium", timeZone: "Asia/Kolkata" });

const card = "rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-34px_rgba(234,88,12,0.5)]";

// Merchant-only: register an invoice for a business customer. The page sits behind the same merchant gate as the
// dashboard, and the endpoints it calls (/merchant/*) check the merchant's sign-in again on the server.
export function RegisterInvoicePage() {
  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <MerchantHeader active="invoices" />
      <main className="mx-auto max-w-[1000px] px-6 py-8">
        <MerchantKeyGate>
          <Body />
        </MerchantKeyGate>
      </main>
    </div>
  );
}

function Body() {
  const invoices = useQuery({ queryKey: ["receivables", "invoices"], queryFn: getInvoices, refetchInterval: 5000 });
  const recent = (invoices.data ?? []).slice(0, 8);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Register an invoice</h1>
        <p className="text-sm text-ink/60 mt-1">
          For one business at a time. Adding a new business also creates its login. Chasing starts by itself from the due
          date. For many invoices at once, use the CSV import on the Receivables tab.
        </p>
      </div>

      <section className={card}>
        <RegisterInvoice />
      </section>

      <section className={card}>
        <h2 className="font-display font-semibold mb-3">Recently registered</h2>
        {recent.length === 0 ? (
          <p className="text-sm text-ink/50">No invoices yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-ink/50">
                <th className="py-1 font-medium">Invoice</th>
                <th className="font-medium">Business</th>
                <th className="font-medium">Due</th>
                <th className="font-medium text-right">Outstanding</th>
                <th className="font-medium pl-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((i) => (
                <tr key={i.id} className="border-t border-ink/5">
                  <td className="py-1.5">{i.invoice_no}</td>
                  <td>{i.company_name}</td>
                  <td className="whitespace-nowrap">{day(i.due_at)}</td>
                  <td className="text-right">{inr(i.outstanding_inr)}</td>
                  <td className="pl-3 text-ink/60">{i.status.replace(/_/g, " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={card}>
        <h2 className="font-display font-semibold mb-3">Business customers</h2>
        <BusinessCustomers />
      </section>
    </div>
  );
}

// The merchant's top bar, shared by the merchant-side pages. "Register invoice" is the highlighted button.
export function MerchantHeader({ active }: { active: "dashboard" | "invoices" | "demo" | "architecture" | "tiers" }) {
  const link = (key: typeof active, to: "/dashboard" | "/demo-console" | "/architecture" | "/tiers", label: string) =>
    active === key ? (
      <span className="px-3 py-1.5 rounded-md text-ink font-semibold">{label}</span>
    ) : (
      <Link to={to} className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">{label}</Link>
    );
  return (
    <header className="border-b border-ink/10 bg-cream/80 sticky top-0 z-10">
      <div className="mx-auto max-w-[1320px] px-6 h-16 flex items-center gap-6">
        <Link to="/" className="font-display font-semibold text-lg tracking-tight">Backstop</Link>
        <nav className="flex items-center gap-1 text-sm">
          {link("dashboard", "/dashboard", "Dashboard")}
          <Link to="/store" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">Storefront</Link>
          {link("architecture", "/architecture", "Architecture")}
          {link("tiers", "/tiers", "Tiers")}
          {link("demo", "/demo-console", "Simulation Console")}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          {active === "invoices" ? <span className="px-4 py-2 rounded-md bg-ink text-cream text-sm font-semibold">+ Register invoice</span> : <RegisterInvoiceButton />}
          <AuthNav />
        </div>
      </div>
    </header>
  );
}

import { Link } from "@tanstack/react-router";

import { TIER_LABEL } from "@/lib/copy";
import type { Customer } from "@/lib/api/auth";

const TIER_BADGE: Record<string, string> = {
  new: "bg-loyal/10 text-loyal",
  casual: "bg-ink/5 text-ink/70",
  regular: "bg-loyal/10 text-loyal",
  loyal: "bg-soft/10 text-soft",
  risk: "bg-failed/10 text-failed",
};

export function StoreHeader({ customer, onLogout }: { customer: Customer; onLogout: () => void }) {
  return (
    <header className="sticky top-0 z-10 border-b border-ink/10 bg-cream/90 backdrop-blur">
      <div className="mx-auto max-w-[1200px] px-6 h-16 flex items-center gap-4">
        <Link to="/" className="flex items-center gap-2.5">
          <span className="font-display font-semibold text-lg tracking-tight">Backstop Store</span>
        </Link>
        <div className="flex-1" />
        <span className="text-sm text-ink/60">{customer.name}</span>
        <span
          className={`text-xs font-medium px-2.5 py-1 rounded-full ${TIER_BADGE[customer.tier] ?? "bg-ink/5 text-ink/70"}`}
        >
          {TIER_LABEL[customer.tier] ?? customer.tier}
        </span>
        <button
          onClick={onLogout}
          className="px-3 py-1.5 rounded-md border border-ink/10 text-sm text-ink/60 hover:text-ink"
        >
          Log out
        </button>
      </div>
    </header>
  );
}

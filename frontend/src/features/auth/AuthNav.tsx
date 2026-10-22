import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";

import { getToken } from "@/lib/auth-token";
import type { Role } from "@/lib/api/auth";
import { useLogout, useWhoAmI } from "@/lib/hooks/useAuth";

export const ROLE_HOME: Record<Role, { to: "/dashboard" | "/account" | "/account/invoices"; label: string }> = {
  merchant: { to: "/dashboard", label: "Dashboard" },
  customer: { to: "/account", label: "My autopay" },
  business: { to: "/account/invoices", label: "My invoices" },
};

/** The sign-in end of every header: Log in / Sign up when nobody is signed in; the person's name, their page and
 *  Log out when someone is. One component so every page behaves the same. */
export function AuthNav() {
  const who = useWhoAmI();
  const logout = useLogout();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const signedIn = !!getToken() && !who.isError;

  if (!signedIn) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Link to="/login" className="px-3 py-1.5 rounded-md text-ink/70 hover:text-ink font-medium">Log in</Link>
        <Link to="/login" search={{ mode: "signup" }} className="px-3 py-1.5 rounded-md bg-ink text-cream font-semibold hover:bg-ink/90">
          Sign up
        </Link>
      </div>
    );
  }
  if (!who.data) return <div className="w-24" />;
  const home = ROLE_HOME[who.data.role];
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="hidden lg:inline text-ink/60 max-w-40 truncate" title={who.data.email}>{who.data.company_name ?? who.data.name}</span>
      <Link to={home.to} className="px-3 py-1.5 rounded-md text-ink/70 hover:text-ink font-medium">{home.label}</Link>
      <button
        type="button"
        className="px-3 py-1.5 rounded-md border border-ink/15 text-ink/70 hover:text-ink"
        onClick={() => { logout(); qc.clear(); navigate({ to: "/login" }); }}
      >
        Log out
      </button>
    </div>
  );
}

/** "+ Register invoice" - the merchant's shortcut. Hidden for everyone else. */
export function RegisterInvoiceButton() {
  const who = useWhoAmI();
  if (!getToken() || who.data?.role !== "merchant") return null;
  return (
    <Link to="/invoices" className="px-4 py-2 rounded-md bg-ember text-white text-sm font-semibold hover:bg-ember/90">
      + Register invoice
    </Link>
  );
}

import type { ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { getToken } from "@/lib/auth-token";
import type { Role } from "@/lib/api/auth";
import { useLogout, useWhoAmI } from "@/lib/hooks/useAuth";

const ROLE_LABEL: Record<Role, string> = { customer: "Customer", business: "Business customer", merchant: "Merchant" };

// Page frame + sign-in guard for the signed-in pages. `role` is the only
// kind of account the page is for; anyone else is told so and pointed home.
export function AccountShell({ role, title, subtitle, children }: { role: Role; title: string; subtitle?: string; children: ReactNode }) {
  const who = useWhoAmI();
  const logout = useLogout();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const signedIn = !!getToken();

  const bar = (
    <header className="border-b border-ink/10 bg-white/70 backdrop-blur">
      <div className="mx-auto max-w-3xl px-4 h-14 flex items-center justify-between gap-3">
        <Link to="/" className="font-display font-semibold tracking-tight">Backstop</Link>
        <div className="flex items-center gap-3 text-sm">
          {who.data && (
            <span className="text-ink/60 hidden sm:inline">
              {who.data.company_name ?? who.data.name} · <span className="text-ink/40">{ROLE_LABEL[who.data.role]}</span>
            </span>
          )}
          {role === "customer" && <Link to="/store" className="text-ink/60 hover:text-ink underline-offset-2 hover:underline">Store</Link>}
          {signedIn && (
            <Button variant="outline" size="sm" onClick={() => { logout(); qc.clear(); navigate({ to: "/login" }); }}>
              Sign out
            </Button>
          )}
        </div>
      </div>
    </header>
  );

  let body: ReactNode;
  if (!signedIn) {
    body = (
      <Card>
        <p className="font-semibold">Please sign in</p>
        <p className="text-sm text-ink/60 mt-1">This page is for signed-in {ROLE_LABEL[role].toLowerCase()} accounts.</p>
        <Button className="mt-4" onClick={() => navigate({ to: "/login" })}>Go to sign in</Button>
      </Card>
    );
  } else if (who.isLoading) {
    body = <p className="text-sm text-ink/50 py-10 text-center">Loading…</p>;
  } else if (who.isError || !who.data) {
    body = (
      <Card>
        <p className="font-semibold">Your sign-in has expired</p>
        <Button className="mt-4" onClick={() => { logout(); navigate({ to: "/login" }); }}>Sign in again</Button>
      </Card>
    );
  } else if (who.data.role !== role) {
    const home = who.data.role === "merchant" ? "/dashboard" : who.data.role === "business" ? "/account/invoices" : "/account";
    body = (
      <Card>
        <p className="font-semibold">This page is not for your account type</p>
        <p className="text-sm text-ink/60 mt-1">You are signed in as {ROLE_LABEL[who.data.role].toLowerCase()}.</p>
        <Button className="mt-4" onClick={() => navigate({ to: home })}>Go to your page</Button>
      </Card>
    );
  } else {
    body = (
      <>
        <h1 className="font-display text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-ink/60 mt-1">{subtitle}</p>}
        <div className="mt-6 space-y-5">{children}</div>
      </>
    );
  }

  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      {bar}
      <main className="mx-auto max-w-3xl px-4 py-8">{body}</main>
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] ${className}`}>
      {children}
    </section>
  );
}

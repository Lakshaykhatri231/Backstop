import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";

import { AuthCard } from "@/features/auth/AuthCard";
import type { Role } from "@/lib/api/auth";

type Purpose = "store" | "autopay" | "invoices" | "merchant";
type LoginSearch = { mode?: "signup"; as?: Purpose; next?: string };

// only these in-app pages may be returned to after sign-in (never an arbitrary URL)
const NEXT_OK = new Set(["/dashboard", "/demo-console", "/invoices", "/architecture", "/tiers"]);

const PURPOSES: { key: Purpose; title: string; blurb: string; signup: boolean }[] = [
  { key: "store", title: "Shop at the store", blurb: "Browse, buy and resume your cart", signup: true },
  { key: "autopay", title: "My autopay", blurb: "Start or manage a recurring payment", signup: true },
  { key: "invoices", title: "My invoices", blurb: "For businesses invoiced by the merchant", signup: false },
  { key: "merchant", title: "Merchant", blurb: "Dashboard, invoices and the console", signup: false },
];

const NO_SIGNUP: Record<string, string> = {
  invoices: "Business accounts are created by the merchant who invoices you. Use the email and one-time password they gave you.",
  merchant: "The merchant account is set up by the store owner. Use the email and password you were given.",
};

export const Route = createFileRoute("/login")({
  validateSearch: (s: Record<string, unknown>): LoginSearch => {
    const out: LoginSearch = {};
    if (s["mode"] === "signup") out.mode = "signup";
    if (["store", "autopay", "invoices", "merchant"].includes(String(s["as"]))) out.as = s["as"] as Purpose;
    if (typeof s["next"] === "string" && NEXT_OK.has(s["next"])) out.next = s["next"];
    return out;
  },
  head: () => ({
    meta: [
      { title: "Sign in — Backstop" },
      { name: "description", content: "One sign-in for the store, autopay, invoices and the merchant dashboard." },
    ],
  }),
  component: Login,
});

function Login() {
  const navigate = useNavigate();
  const { mode, as, next } = Route.useSearch();
  const qc = useQueryClient();
  const purpose: Purpose = as ?? "store";
  const current = PURPOSES.find((p) => p.key === purpose)!;
  const signup = current.signup && mode === "signup";

  // The account decides where you land; the tile above only picks what a SHOPPER is here for.
  const go = (role: Role) => {
    qc.removeQueries();                       // nothing cached while signed out may be shown as the new account's
    if (role === "merchant" && next) {
      navigate({ to: next as "/dashboard" });
      return;
    }
    navigate({
      to: role === "merchant" ? "/dashboard" : role === "business" ? "/account/invoices" : purpose === "autopay" ? "/account" : "/store",
    });
  };

  return (
    <div className="relative min-h-screen w-full overflow-hidden text-cream font-body">
      <div className="absolute inset-0 -z-10 bg-gradient-to-br from-[#3a1f0a] via-[#7c3f12] to-[#d97706]" />
      <div className="absolute -top-24 -right-24 -z-10 size-96 rounded-full bg-amber-300/40 blur-3xl" />
      <div className="mx-auto max-w-[1200px] px-6 py-10 flex min-h-screen items-center">
        <div className="w-full max-w-xl mx-auto">
          <Link to="/" className="flex items-center gap-2.5 mb-8">
            <span className="font-display font-semibold text-xl tracking-tight">Backstop</span>
          </Link>
          <h1 className="font-display text-3xl font-semibold tracking-tight">{signup ? "Create your account" : "Welcome back"}</h1>
          <p className="text-cream/70 text-sm mt-2">One place to sign in. Choose what you are here for.</p>

          <div className="mt-6 grid grid-cols-2 gap-2">
            {PURPOSES.map((p) => (
              <Link
                key={p.key}
                to="/login"
                search={{ as: p.key, ...(mode === "signup" && p.signup ? { mode: "signup" as const } : {}), ...(next ? { next } : {}) }}
                replace
                className={`rounded-xl border px-3 py-3 text-left transition-colors ${
                  p.key === purpose ? "border-tangerine bg-tangerine/15" : "border-cream/20 bg-cream/5 hover:bg-cream/10"
                }`}
              >
                <span className="block text-sm font-semibold">{p.title}</span>
                <span className="block text-xs text-cream/60 mt-0.5">{p.blurb}</span>
              </Link>
            ))}
          </div>

          <AuthCard
            key={`${purpose}-${current.signup}-${mode ?? ""}`}
            className="mt-6"
            allowRegister={current.signup}
            initialMode={mode === "signup" ? "register" : "login"}
            onAuthed={go}
          />
          {!current.signup && NO_SIGNUP[purpose] && <p className="text-xs text-cream/60 mt-4">{NO_SIGNUP[purpose]}</p>}
          <p className="text-xs text-cream/50 mt-8 text-center">Demo storefront — real Razorpay test-mode checkout.</p>
        </div>
      </div>
    </div>
  );
}

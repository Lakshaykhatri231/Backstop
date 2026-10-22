import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";

import { apiFetch } from "@/lib/api/client";
import { getToken } from "@/lib/auth-token";
import { useLogout, useWhoAmI } from "@/lib/hooks/useAuth";
import { clearMerchantKey, setMerchantKey } from "@/lib/merchant-key";

type AuthStatus = { required: boolean; ok: boolean; key_configured?: boolean; merchant_login?: boolean };

// Pages for the merchant only. There is ONE sign-in page (/login); this gate never shows its own form. Nobody
// signed in -> sent to /login?as=merchant and brought back here afterwards. Signed in as someone else ->
// told so, with a way to switch. If the server has an access key (MERCHANT_API_KEY) it can still be used, as
// a small fallback underneath. With no merchant account and no key the pages stay open (a fresh clone demos).
export function MerchantKeyGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const logout = useLogout();
  const signedIn = !!getToken();
  const statusQ = useQuery({
    queryKey: ["merchant", "auth-status", signedIn],
    queryFn: () => apiFetch<AuthStatus>("/merchant/auth-status"),
    retry: false,
    staleTime: 0,
    refetchOnMount: "always",
  });
  const who = useWhoAmI();
  const [value, setValue] = useState("");
  const [tried, setTried] = useState(false);

  const blocked = !!statusQ.data && statusQ.data.required && !statusQ.data.ok && !statusQ.isFetching;
  const nobody = blocked && (!signedIn || who.isError);
  useEffect(() => {
    if (nobody && !statusQ.data?.key_configured) {
      const next = window.location.pathname;
      navigate({ to: "/login", search: { as: "merchant", next } as never, replace: true });
    }
  }, [nobody, statusQ.data?.key_configured, navigate]);

  if (statusQ.isLoading || (statusQ.isFetching && !statusQ.data)) {
    return <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>;
  }
  // If the status call itself fails, fall through: the page's own queries will surface the real error.
  if (!statusQ.data || !statusQ.data.required || statusQ.data.ok) return <>{children}</>;
  if (statusQ.isFetching) return <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>;

  async function submitKey(e: React.FormEvent) {
    e.preventDefault();
    setMerchantKey(value.trim());
    setTried(true);
    await qc.invalidateQueries();
  }

  const other = signedIn && who.data && who.data.role !== "merchant";
  return (
    <div className="max-w-sm mx-auto mt-16 rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] space-y-5">
      {other ? (
        <div>
          <h1 className="font-display text-xl font-semibold">This page is for the merchant</h1>
          <p className="text-sm text-ink/60 mt-1">You are signed in as {who.data!.email} ({who.data!.role}).</p>
          <button
            type="button"
            className="mt-4 w-full rounded-md bg-ink text-cream py-2 text-sm font-semibold"
            onClick={() => { logout(); qc.clear(); navigate({ to: "/login", search: { as: "merchant", next: window.location.pathname } as never }); }}
          >
            Sign in as the merchant
          </button>
        </div>
      ) : (
        <div>
          <h1 className="font-display text-xl font-semibold">Taking you to sign in…</h1>
          <p className="text-sm text-ink/60 mt-1">This page needs the merchant account.</p>
        </div>
      )}
      {statusQ.data.key_configured && (
        <div>
          <h2 className="text-sm font-semibold">Or use the access key</h2>
          <form onSubmit={submitKey} className="mt-2 space-y-2">
            <input type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)}
              className="w-full rounded-md border border-ink/15 px-3 py-2 text-sm" placeholder="Access key (MERCHANT_API_KEY)" />
            {tried && <p className="text-sm text-failed">That key was not accepted.</p>}
            <button type="submit" disabled={!value.trim()}
              className="w-full rounded-md border border-ink/20 py-2 text-sm font-semibold disabled:opacity-50">Unlock</button>
          </form>
          <button type="button" className="mt-2 text-xs text-ink/40 underline" onClick={() => { clearMerchantKey(); qc.invalidateQueries(); }}>
            Clear saved key
          </button>
        </div>
      )}
    </div>
  );
}

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  cancelMandate, chargeNext, createAutopayScenario, getLinkUsage, listMandates, syncMandate,
} from "@/lib/api/autopay";
import { ApiError } from "@/lib/api/client";

import { fmtIst, inr } from "./format";

const STATUS_STYLE: Record<string, string> = {
  setup: "bg-declined/10 text-declined",
  active: "bg-soft/10 text-soft",
  rejected: "bg-failed/10 text-failed",
  cancelled: "bg-failed/10 text-failed",
  paused: "bg-declined/10 text-declined",
};

// Everything in this panel is REAL Razorpay Test Mode: the links and mandates
// exist on the merchant's Razorpay account, and payments arrive by webhook.
export function RazorpayPanel() {
  const qc = useQueryClient();
  const usage = useQuery({ queryKey: ["razorpay", "usage"], queryFn: getLinkUsage, refetchInterval: 15000 });
  const mandates = useQuery({ queryKey: ["razorpay", "mandates"], queryFn: listMandates, refetchInterval: 5000 });
  const [name, setName] = useState("Demo Autopay Customer");
  const [phone, setPhone] = useState("9876543210");
  const [amount, setAmount] = useState(499);
  const [error, setError] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["razorpay"] }).then(() => qc.invalidateQueries({ queryKey: ["demo"] }));
  const onErr = (e: unknown) => setError(e instanceof ApiError ? e.message : String(e));

  const [created, setCreated] = useState<string | null>(null);
  // The tab is opened synchronously on the click (a tab opened after the
  // request returns is treated as a popup and silently blocked), then pointed
  // at Razorpay's page once it exists.
  const create = useMutation({
    mutationFn: (_tab: Window | null) => createAutopayScenario({ name, phone, amount_inr: amount }),
    onSuccess: (r, tab) => {
      setError(null);
      setCreated(r.registration_url);
      if (tab && r.registration_url) tab.location.href = r.registration_url;
      else tab?.close();
      refresh();
    },
    onError: (e, tab) => { tab?.close(); onErr(e); },
  });
  const sync = useMutation({ mutationFn: syncMandate, onSuccess: refresh, onError: onErr });
  const charge = useMutation({ mutationFn: chargeNext, onSuccess: refresh, onError: onErr });
  const cancel = useMutation({ mutationFn: cancelMandate, onSuccess: refresh, onError: onErr });

  const u = usage.data;
  const used = u?.account_links_total ?? u?.real_links_created_here ?? 0;
  return (
    <div className="space-y-4">
      {u && (
        <div className="rounded-xl border border-ink/10 px-3 py-2.5 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">Razorpay Payment Links</span>
            <span className={`text-xs px-2 py-0.5 rounded-full ${u.mode === "real" ? "bg-soft/10 text-soft" : "bg-declined/10 text-declined"}`}>
              {u.mode === "real" ? (u.test_mode ? "real · Test Mode" : "real") : "stand-ins only"}
            </span>
          </div>
          {u.test_mode && (
            <>
              <div className="mt-2 h-2 rounded-full bg-ink/10 overflow-hidden">
                <div className={`h-full ${used >= u.cap ? "bg-failed" : used >= u.cap * 0.8 ? "bg-declined" : "bg-soft"}`}
                  style={{ width: `${Math.min(100, (used / u.cap) * 100)}%` }} />
              </div>
              <p className="text-xs text-ink/60 mt-1">
                {used} of ~{u.cap} Test Mode links used on the account ({u.real_links_created_here} made by this app).
                Links are only made when a message needs one, and reused.
                {u.limit_hits > 0 && <> <strong className="text-failed">{u.limit_hits} hit the limit</strong> and went out as labelled stand-ins.</>}
              </p>
            </>
          )}
        </div>
      )}

      <div>
        <p className="text-sm text-ink/70 mb-2">
          <strong>Real card autopay.</strong> Creates a mandate on Razorpay and opens its page. Authorise with test card{" "}
          <code>4100 2800 0000 1007</code> (later debits succeed) or <code>4718 6000 0007 0021</code> (later debits fail:
          insufficient funds — that opens a recovery run here). Any CVV, any future expiry.
        </p>
        <div className="grid sm:grid-cols-3 gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} className="rounded-md border border-ink/15 px-2 py-1.5 text-sm" />
          <input value={phone} onChange={(e) => setPhone(e.target.value)} className="rounded-md border border-ink/15 px-2 py-1.5 text-sm" />
          <input type="number" value={amount} onChange={(e) => setAmount(Number(e.target.value))}
            className="rounded-md border border-ink/15 px-2 py-1.5 text-sm" />
        </div>
        <button type="button" onClick={() => { setCreated(null); create.mutate(window.open("about:blank", "_blank")); }}
          disabled={create.isPending}
          className="mt-2 px-4 py-2 rounded-md bg-ember text-white text-sm font-semibold disabled:opacity-50">
          {create.isPending ? "Creating on Razorpay…" : "Create real autopay mandate"}
        </button>
        {error && (
          <p className="mt-2 text-sm rounded-md bg-failed/10 text-failed px-3 py-2">Could not create the mandate: {error}</p>
        )}
        {created && (
          <p className="mt-2 text-sm rounded-md bg-soft/10 text-soft px-3 py-2">
            Mandate created on Razorpay.{" "}
            <a className="underline font-medium" href={created} target="_blank" rel="noreferrer">Open the authorisation page ↗</a>
          </p>
        )}
      </div>

      {(mandates.data ?? []).length > 0 && (
        <ul className="space-y-2">
          {mandates.data!.map((m) => (
            <li key={m.id} className="rounded-lg border border-ink/10 px-3 py-2 text-sm">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium">{m.customer_name}</span>
                <span className="text-ink/50">{inr(m.amount_inr)} / {m.frequency}</span>
                <span className={`text-[11px] px-1.5 py-0.5 rounded-full ${STATUS_STYLE[m.status] ?? ""}`}>{m.status}</span>
                {m.next_due_at && m.status === "active" && (
                  <span className="text-xs text-ink/50">next debit {fmtIst(m.next_due_at)} IST</span>
                )}
              </div>
              <div className="mt-1.5 flex gap-2 flex-wrap text-xs">
                {m.status === "setup" && m.registration_url && (
                  <a className="px-2 py-1 rounded border border-ink/20 underline" href={m.registration_url} target="_blank" rel="noreferrer">
                    Authorise on Razorpay ↗
                  </a>
                )}
                {m.status !== "cancelled" && (
                  <button type="button" className="px-2 py-1 rounded border border-ink/20" onClick={() => sync.mutate(m.id)}>
                    Check Razorpay now
                  </button>
                )}
                {m.status === "active" && (
                  <>
                    <button type="button" className="px-2 py-1 rounded border border-ink/20" onClick={() => charge.mutate(m.id)}>
                      Queue next debit (notice now, debit +37h)
                    </button>
                    <button type="button" className="px-2 py-1 rounded border border-failed/30 text-failed" onClick={() => cancel.mutate(m.id)}>
                      Cancel mandate
                    </button>
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-ink/50">
        Test Mode sends no bank pre-debit notice and settles a debit about 5 minutes after it is made; results arrive by
        webhook (needs the tunnel) — or press “Check Razorpay now” for the mandate state.
      </p>
    </div>
  );
}

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  cancelAutopay, getServices, listMyAutopay, saveAutopayPermissions, startAutopay, syncAutopay,
  type Autopay, type ServicesResponse,
} from "@/lib/api/account";
import { sendProfileOtp, verifyProfilePhone } from "@/lib/api/contact";

import { AccountShell, Card } from "./AccountShell";
import { errMessage, NotesList, PermissionsCard } from "./PermissionsCard";

const inr = (n: number | null | undefined) => (n == null ? "—" : `₹${Math.round(n).toLocaleString("en-IN")}`);
const when = (iso: string | null) =>
  iso ? new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" }) + " IST" : "—";
const per = (f: string) => (f === "weekly" ? "week" : f === "yearly" ? "year" : "month");

// The customer's autopay page: pick a service -> authorise on Razorpay -> give
// the permissions we ask for -> see when the next debit happens and what to
// expect. Everything shown comes from the server; this page decides nothing.
export function AutopayPage() {
  return (
    <AccountShell role="customer" title="Your autopay" subtitle="Pay for a service automatically on a schedule. You stay in control and can cancel any time.">
      <AutopayBody />
    </AccountShell>
  );
}

function AutopayBody() {
  const qc = useQueryClient();
  const services = useQuery({ queryKey: ["account", "services"], queryFn: getServices, staleTime: Infinity });
  const mine = useQuery({
    queryKey: ["account", "autopay"],
    queryFn: listMyAutopay,
    // while a setup is waiting on Razorpay, ask it every few seconds
    refetchInterval: (q) => ((q.state.data ?? []).some((m) => m.status === "setup") ? 5000 : false),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["account", "autopay"] });

  const list = mine.data ?? [];
  const current = list.find((m) => m.status === "active") ?? list.find((m) => m.status === "setup");
  const past = list.filter((m) => m !== current && (m.status === "cancelled" || m.status === "rejected"));

  return (
    <>
      {current?.status === "active" ? (
        <ActiveAutopay m={current} consent={services.data?.consent_text} onChanged={refresh} testMode={!!services.data?.test_mode} />
      ) : current?.status === "setup" ? (
        <Authorise m={current} onChanged={refresh} />
      ) : (
        services.data && <Chooser data={services.data} onStarted={refresh} />
      )}
      {past.length > 0 && (
        <Card>
          <h2 className="text-xs uppercase tracking-[0.15em] text-ink/50 mb-2">Earlier</h2>
          <ul className="text-sm space-y-1">
            {past.map((m) => (
              <li key={m.id} className="flex justify-between">
                <span>{m.service_name} · {inr(m.amount_inr)}/{per(m.frequency)}</span>
                <span className="text-ink/50">{m.status}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}

// ── Step 1: choose ──

function Chooser({ data, onStarted }: { data: ServicesResponse; onStarted: () => void }) {
  const [choice, setChoice] = useState<string>(data.services[0]?.key ?? "plus");
  const [amount, setAmount] = useState("250");
  const [freq, setFreq] = useState("monthly");
  const [error, setError] = useState<string | null>(null);
  const custom = choice === data.custom.key;

  // The tab is opened on the click itself (a tab opened after the request
  // returns is blocked as a popup), then pointed at Razorpay's page.
  const start = useMutation({
    mutationFn: (_tab: Window | null) =>
      startAutopay(custom ? { service_key: choice, amount_inr: Number(amount), frequency: freq } : { service_key: choice }),
    onSuccess: (m, tab) => {
      setError(null);
      if (tab && m.registration_url) tab.location.href = m.registration_url;
      else tab?.close();
      onStarted();
    },
    onError: (e, tab) => { tab?.close(); setError(errMessage(e)); },
  });

  return (
    <Card>
      <h2 className="font-display text-lg font-semibold">1 · Choose what to pay for</h2>
      <div className="mt-3 grid gap-2">
        {data.services.map((s) => (
          <label key={s.key} className={`flex items-center gap-3 rounded-xl border px-3 py-3 cursor-pointer ${choice === s.key ? "border-ember bg-ember/5" : "border-ink/10"}`}>
            <input type="radio" name="svc" checked={choice === s.key} onChange={() => setChoice(s.key)} />
            <span className="flex-1">
              <span className="font-semibold">{s.name}</span>
              <span className="block text-xs text-ink/60">{s.blurb}</span>
            </span>
            <span className="font-semibold">{inr(s.amount_inr)}<span className="text-xs font-normal text-ink/50"> / {per(s.frequency)}</span></span>
          </label>
        ))}
        <label className={`rounded-xl border px-3 py-3 cursor-pointer ${custom ? "border-ember bg-ember/5" : "border-ink/10"}`}>
          <span className="flex items-center gap-3">
            <input type="radio" name="svc" checked={custom} onChange={() => setChoice(data.custom.key)} />
            <span className="flex-1 font-semibold">Your own amount</span>
          </span>
          {custom && (
            <span className="mt-3 flex flex-wrap items-center gap-2 text-sm">
              ₹<Input className="w-28" inputMode="numeric" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ""))} />
              every
              <select value={freq} onChange={(e) => setFreq(e.target.value)} className="rounded-md border border-ink/15 bg-white px-2 py-2">
                {data.custom.frequencies.map((f) => <option key={f} value={f}>{per(f)}</option>)}
              </select>
              <span className="text-xs text-ink/50">between ₹{data.custom.min_inr} and ₹{data.custom.max_inr}</span>
            </span>
          )}
        </label>
      </div>
      <Button
        className="mt-4 w-full"
        disabled={start.isPending || (custom && !Number(amount))}
        onClick={() => start.mutate(window.open("", "_blank"))}
      >
        {start.isPending ? "Opening Razorpay…" : "Continue to Razorpay to authorise"}
      </Button>
      <p className="text-[11px] text-ink/50 mt-2">
        You authorise your card once on Razorpay's page. {data.test_mode && "This is Razorpay Test Mode: use a Razorpay test card, no real money moves."}
      </p>
      {error && <p className="mt-3 text-sm text-failed">{error}</p>}
    </Card>
  );
}

// ── Step 2: waiting for Razorpay ──

function Authorise({ m, onChanged }: { m: Autopay; onChanged: () => void }) {
  const sync = useMutation({ mutationFn: () => syncAutopay(m.id), onSuccess: onChanged });
  // Razorpay gives no "come back to us" step after a card authorisation, so we ask it.
  useEffect(() => {
    const t = setInterval(() => sync.mutate(), 8000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [m.id]);
  const cancel = useMutation({ mutationFn: () => cancelAutopay(m.id), onSuccess: onChanged });
  return (
    <Card>
      <h2 className="font-display text-lg font-semibold">2 · Authorise on Razorpay</h2>
      <p className="text-sm text-ink/70 mt-1">
        {m.service_name} · {inr(m.amount_inr)} every {per(m.frequency)}. Finish the authorisation in the Razorpay tab, then come back here.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        {m.registration_url && (
          <Button asChild><a href={m.registration_url} target="_blank" rel="noreferrer">Open Razorpay again</a></Button>
        )}
        <Button variant="outline" disabled={sync.isPending} onClick={() => sync.mutate()}>
          {sync.isPending ? "Checking…" : "I have authorised"}
        </Button>
        <Button variant="ghost" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Start over</Button>
      </div>
      <p className="text-xs text-ink/50 mt-3">We check with Razorpay every few seconds. This page updates by itself once it is confirmed.</p>
      {sync.isError && <p className="text-sm text-failed mt-2">{errMessage(sync.error)}</p>}
    </Card>
  );
}

// ── Step 3 + overview: permissions, then the schedule ──

function ActiveAutopay({ m, consent, onChanged, testMode }: {
  m: Autopay; consent: ServicesResponse["consent_text"] | undefined; onChanged: () => void; testMode: boolean;
}) {
  const cancel = useMutation({ mutationFn: () => cancelAutopay(m.id), onSuccess: onChanged });
  const [confirmCancel, setConfirmCancel] = useState(false);
  const asked = m.contact.phone_verified && (m.contact.voice_consent || m.contact.memory_consent);
  const [showPerms, setShowPerms] = useState(!asked);

  return (
    <>
      <Card>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.15em] text-soft font-semibold">Autopay is on</p>
            <h2 className="font-display text-xl font-semibold mt-1">{m.service_name}</h2>
            <p className="text-sm text-ink/60">{inr(m.amount_inr)} every {per(m.frequency)}</p>
          </div>
          <span className="rounded-full bg-soft/10 text-soft text-xs font-semibold px-2.5 py-1">active</span>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
          <div className="rounded-xl bg-sand/50 p-3">
            <dt className="text-xs text-ink/50">Next debit</dt>
            <dd className="font-semibold">{when(m.next_debit_at)}</dd>
            <dd className="text-ink/70">{inr(m.next_debit_amount_inr)}</dd>
          </div>
          <div className="rounded-xl bg-sand/50 p-3">
            <dt className="text-xs text-ink/50">We confirm with you by</dt>
            <dd className="font-semibold">{when(m.notice_at)}</dd>
            <dd className="text-ink/70 text-xs">at least 37 hours before the debit</dd>
          </div>
        </dl>
        {m.history.length > 0 && (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-[0.15em] text-ink/50 mb-1">Debits so far</h3>
            <ul className="text-sm divide-y divide-ink/5">
              {m.history.map((h, i) => (
                <li key={i} className="py-1.5 flex justify-between">
                  <span>{when(h.at)}</span>
                  <span>{inr(h.amount_inr)} · <span className={h.status === "paid" ? "text-soft" : h.status === "refused" ? "text-failed" : "text-ink/60"}>{h.status}</span></span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="mt-4 pt-4 border-t border-ink/5 flex items-center justify-between">
          {!confirmCancel ? (
            <Button variant="outline" size="sm" onClick={() => setConfirmCancel(true)}>Cancel autopay</Button>
          ) : (
            <span className="flex items-center gap-2 text-sm">
              Stop all future debits?
              <Button size="sm" variant="destructive" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Yes, cancel</Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmCancel(false)}>No</Button>
            </span>
          )}
          {testMode && <span className="text-[11px] text-ink/40">Razorpay Test Mode</span>}
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between">
          <h2 className="font-display text-lg font-semibold">{asked && !showPerms ? "What to expect" : "3 · Your permissions"}</h2>
          {asked && (
            <button type="button" className="text-xs underline text-ink/60" onClick={() => setShowPerms((v) => !v)}>
              {showPerms ? "Hide" : "Change my choices"}
            </button>
          )}
        </div>
        {showPerms ? (
          <div className="mt-3">
            <PermissionsCard
              contact={m.contact}
              consentText={consent}
              notes={m.notes}
              save={saveAutopayPermissions}
              sendOtp={sendProfileOtp}
              verifyPhone={verifyProfilePhone}
              onChanged={onChanged}
              intro="Your autopay is confirmed. If a debit ever fails, these choices decide how we reach you. Nothing is ticked for you."
            />
          </div>
        ) : (
          <div className="mt-3"><NotesList notes={m.notes} /></div>
        )}
      </Card>
    </>
  );
}

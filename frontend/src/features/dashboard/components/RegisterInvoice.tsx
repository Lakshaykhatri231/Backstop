import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createBusinessCustomer, listBusinessCustomers, registerInvoice } from "@/lib/api/account";
import { ApiError } from "@/lib/api/client";
import { LANGUAGE_LABEL, type Language } from "@/lib/api/contact";

const msg = (e: unknown) => {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: unknown } | null)?.detail;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    if (typeof d === "string") return d;
  }
  return e instanceof Error ? e.message : "Something went wrong";
};

// Register one invoice for one business: pick the business (or add it, which
// also creates its login) and give an amount and a due date. The merchant can
// enter a number and language but never the customer's consent - the business
// gives that itself, on its own page, after verifying the number.
export function RegisterInvoice() {
  const qc = useQueryClient();
  const customers = useQuery({ queryKey: ["merchant", "business-customers"], queryFn: listBusinessCustomers });
  const [accountId, setAccountId] = useState("");
  const [adding, setAdding] = useState(false);
  const [co, setCo] = useState({ company_name: "", contact_name: "", email: "", phone: "", language: "hinglish" as Language });
  const [amount, setAmount] = useState("");
  const [due, setDue] = useState("");
  const [invNo, setInvNo] = useState("");
  const [done, setDone] = useState<{ text: string; secret?: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["merchant", "business-customers"] });
    qc.invalidateQueries(); // invoice lists, receivables summary
  };

  const addCo = useMutation({
    mutationFn: () => createBusinessCustomer({
      company_name: co.company_name.trim(), email: co.email.trim(),
      ...(co.contact_name.trim() ? { contact_name: co.contact_name.trim() } : {}),
      ...(co.phone.trim() ? { phone: co.phone.trim() } : {}),
      preferred_language: co.language,
    }),
    onSuccess: (r) => {
      setError(null);
      setAccountId(r.account_id);
      setAdding(false);
      setDone({ text: `${r.company_name} added - this does NOT create an invoice yet. Their login: ${r.login_email} · one-time password:`, secret: r.temporary_password });
      refresh();
    },
    onError: (e) => setError(msg(e)),
  });

  const addInv = useMutation({
    mutationFn: () => registerInvoice({
      account_id: accountId, amount_inr: Number(amount), due_at: new Date(due + "T09:00:00+05:30").toISOString(),
      ...(invNo.trim() ? { invoice_no: invNo.trim() } : {}),
    }),
    onSuccess: (r) => {
      setError(null);
      setDone({ text: `Invoice ${r.invoice_no} registered for ₹${Math.round(r.amount_inr).toLocaleString("en-IN")}. Chasing starts by itself from its due date.` });
      setAmount(""); setInvNo("");
      refresh();
    },
    onError: (e) => setError(msg(e)),
  });

  const list = customers.data ?? [];
  const canInvoice = accountId && Number(amount) > 0 && due;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <span className="block text-xs text-ink/50 mb-1">Business</span>
          <select value={accountId} onChange={(e) => { setAccountId(e.target.value); setAdding(false); }}
            className="rounded-md border border-ink/15 bg-white px-2 py-2 text-sm min-w-56">
            <option value="">Choose a business…</option>
            {list.map((c) => <option key={c.id} value={c.id}>{c.company_name}{c.login_email ? "" : " (no login)"}</option>)}
          </select>
        </label>
        <Button variant="outline" size="sm" onClick={() => { setAdding((v) => !v); setAccountId(""); }}>
          {adding ? "Cancel" : "+ New business"}
        </Button>
      </div>

      {adding && (
        <div className="rounded-xl border border-ink/10 p-3 grid gap-2 sm:grid-cols-2">
          <Input placeholder="Company name" value={co.company_name} onChange={(e) => setCo({ ...co, company_name: e.target.value })} />
          <Input placeholder="Contact person" value={co.contact_name} onChange={(e) => setCo({ ...co, contact_name: e.target.value })} />
          <Input type="email" placeholder="Login email" value={co.email} onChange={(e) => setCo({ ...co, email: e.target.value })} />
          <Input inputMode="tel" placeholder="Mobile number" value={co.phone} onChange={(e) => setCo({ ...co, phone: e.target.value })} />
          <select value={co.language} onChange={(e) => setCo({ ...co, language: e.target.value as Language })}
            className="rounded-md border border-ink/15 bg-white px-2 py-2 text-sm">
            {(Object.keys(LANGUAGE_LABEL) as Language[]).map((l) => <option key={l} value={l}>{LANGUAGE_LABEL[l]}</option>)}
          </select>
          <Button disabled={addCo.isPending || !co.company_name.trim() || !co.email.trim()} onClick={() => addCo.mutate()}>
            {addCo.isPending ? "Adding…" : "Add business + login"}
          </Button>
          <p className="sm:col-span-2 text-[11px] text-ink/50">
            The number is unverified and call consent is off until the business verifies and agrees on its own page.
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm"><span className="block text-xs text-ink/50 mb-1">Amount (₹)</span>
          <Input className="w-32" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ""))} /></label>
        <label className="text-sm"><span className="block text-xs text-ink/50 mb-1">Due date</span>
          <Input type="date" value={due} onChange={(e) => setDue(e.target.value)} /></label>
        <label className="text-sm"><span className="block text-xs text-ink/50 mb-1">Invoice no. (optional)</span>
          <Input className="w-40" placeholder="auto" value={invNo} onChange={(e) => setInvNo(e.target.value)} /></label>
        <Button disabled={!canInvoice || addInv.isPending} onClick={() => addInv.mutate()}>
          {addInv.isPending ? "Registering…" : "Register invoice"}
        </Button>
      </div>
      {!canInvoice && (
        <p className="text-xs text-ink/50">
          To register an invoice: {[!accountId && "choose a business", !(Number(amount) > 0) && "enter an amount", !due && "pick a due date"].filter(Boolean).join(", ")}.
        </p>
      )}

      {done && (
        <div className="rounded-lg bg-soft/10 text-sm px-3 py-2">
          {done.text} {done.secret && <code className="font-mono font-bold select-all">{done.secret}</code>}
          {done.secret && <span className="block text-[11px] text-ink/60">Shown once - copy it now and hand it to the business.</span>}
        </div>
      )}
      {error && <p className="text-sm text-failed">{error}</p>}
    </div>
  );
}

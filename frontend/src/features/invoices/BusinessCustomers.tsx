import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  listBusinessCustomers, resetBusinessPassword, updateBusinessCustomer, type BusinessCustomer,
} from "@/lib/api/account";
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

// The merchant corrects what it typed about a business: name, email (which is also the login), number, language.
// It never touches what the BUSINESS decides - call consent and memory - and a new number must be verified again.
export function BusinessCustomers() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["merchant", "business-customers"], queryFn: listBusinessCustomers });
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ ok: boolean; text: string; secret?: string } | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["merchant", "business-customers"] });
  const list = q.data ?? [];

  return (
    <div className="space-y-3">
      {notice && (
        <div className={`rounded-lg px-3 py-2 text-sm ${notice.ok ? "bg-soft/10" : "bg-failed/10 text-failed"}`}>
          {notice.text} {notice.secret && <code className="font-mono font-bold select-all">{notice.secret}</code>}
          {notice.secret && <span className="block text-[11px] text-ink/60">Shown once - copy it and hand it to the business.</span>}
        </div>
      )}
      {list.length === 0 ? (
        <p className="text-sm text-ink/50">None yet. Add one with “+ New business” above.</p>
      ) : (
        <ul className="divide-y divide-ink/5">
          {list.map((c) => (
            <li key={c.id} className="py-2">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                <span className="font-semibold min-w-40">{c.company_name}</span>
                <span className="text-ink/70">{c.login_email ?? <span className="text-ink/40">no login</span>}</span>
                <span className="text-ink/70">{c.phone_masked || "no number"} {c.phone_verified && <span className="text-soft">✓ verified</span>}</span>
                <span className={c.can_call ? "text-soft" : "text-ink/50"}>{c.can_call ? "can be called" : "no call permission yet"}</span>
                <button type="button" className="ml-auto text-xs underline text-ink/60"
                  onClick={() => { setEditing(editing === c.id ? null : c.id); setNotice(null); }}>
                  {editing === c.id ? "Close" : "Edit"}
                </button>
              </div>
              {editing === c.id && (
                <EditForm c={c} onSaved={(t) => { setNotice({ ok: true, text: t }); setEditing(null); refresh(); }}
                  onPassword={(t, secret) => setNotice({ ok: true, text: t, secret })} onError={(t) => setNotice({ ok: false, text: t })} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EditForm({ c, onSaved, onPassword, onError }: {
  c: BusinessCustomer; onSaved: (t: string) => void; onPassword: (t: string, secret: string) => void; onError: (t: string) => void;
}) {
  const [f, setF] = useState({
    company_name: c.company_name, contact_name: c.contact_name ?? "", email: c.login_email ?? c.email ?? "",
    phone: c.phone ?? "", language: c.preferred_language, terms: String(c.payment_terms_days),
  });
  const phoneChanged = (f.phone || "").replace(/\D/g, "").slice(-10) !== (c.phone ?? "").replace(/\D/g, "").slice(-10);
  const save = useMutation({
    mutationFn: () => updateBusinessCustomer(c.id, {
      company_name: f.company_name, contact_name: f.contact_name, email: f.email,
      ...(f.phone.trim() ? { phone: f.phone.trim() } : {}),
      preferred_language: f.language as Language, payment_terms_days: Number(f.terms),
    }),
    onSuccess: (r) => onSaved(r.verification_and_consent_cleared
      ? `${c.company_name} updated. The new number is unverified and call permission is off until they verify and agree again.`
      : `${c.company_name} updated.`),
    onError: (e) => onError(msg(e)),
  });
  const reset = useMutation({
    mutationFn: () => resetBusinessPassword(c.id),
    onSuccess: (r) => onPassword(`New password for ${r.login_email}:`, r.temporary_password),
    onError: (e) => onError(msg(e)),
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  return (
    <div className="mt-2 rounded-xl border border-ink/10 bg-cream p-3 grid gap-2 sm:grid-cols-2">
      <label className="text-xs text-ink/60">Company<Input value={f.company_name} onChange={set("company_name")} /></label>
      <label className="text-xs text-ink/60">Contact person<Input value={f.contact_name} onChange={set("contact_name")} /></label>
      <label className="text-xs text-ink/60">Email (also their login)<Input type="email" value={f.email} onChange={set("email")} /></label>
      <label className="text-xs text-ink/60">Mobile number<Input inputMode="tel" value={f.phone} onChange={set("phone")} /></label>
      <label className="text-xs text-ink/60">Language
        <select value={f.language} onChange={set("language")} className="mt-1 w-full rounded-md border border-ink/15 bg-white px-2 py-2 text-sm text-ink">
          {(Object.keys(LANGUAGE_LABEL) as Language[]).map((l) => <option key={l} value={l}>{LANGUAGE_LABEL[l]}</option>)}
        </select>
      </label>
      <label className="text-xs text-ink/60">Payment terms (days)<Input inputMode="numeric" value={f.terms} onChange={set("terms")} /></label>
      {phoneChanged && (
        <p className="sm:col-span-2 text-xs rounded bg-declined/15 text-declined px-2 py-1">
          A new number must be verified by the business again, and any call permission they gave is switched off.
        </p>
      )}
      <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={save.isPending || !f.company_name.trim() || !f.email.trim()} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save changes"}
        </Button>
        {c.login_email && (
          <Button size="sm" variant="outline" disabled={reset.isPending}
            onClick={() => { if (window.confirm("Issue a new one-time password? The old one stops working.")) reset.mutate(); }}>
            Reset their password
          </Button>
        )}
        <span className="text-[11px] text-ink/50">Call consent and memory consent are theirs to change, not yours.</span>
      </div>
    </div>
  );
}

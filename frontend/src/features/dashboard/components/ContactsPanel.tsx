import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError } from "@/lib/api/client";
import {
  DEFAULT_LANGUAGE,
  getContacts,
  getMandateRuns,
  LANGUAGE_LABEL,
  setContact,
  type ContactStatus,
  type Language,
} from "@/lib/api/contact";
import { getAccounts } from "@/lib/api/receivables";
import type { Tone } from "@/lib/constants/tone";

import { ToneBadge } from "./ToneBadge";

// What the merchant sees about a customer's callability. The merchant can
// supply the number and language; consent can only come from the customer
// (their own tick, after a code check), so there is no consent control here.
export function contactBadge(c: ContactStatus): { label: string; tone: Tone } {
  if (!c.has_phone) return { label: "No number", tone: "neutral" };
  if (c.block_reason === "wrong_number_flagged") return { label: "Wrong number", tone: "failed" };
  if (!c.phone_verified) return { label: "Not verified", tone: "declined" };
  if (!c.voice_consent) return { label: "Verified · no consent", tone: "loyal" };
  return { label: "Can call", tone: "soft" };
}

function copyLink(path: string) {
  navigator.clipboard?.writeText(`${window.location.origin}${path}`);
}

export function ContactsPanel() {
  const qc = useQueryClient();
  const contactsQ = useQuery({ queryKey: ["contacts", "list"], queryFn: getContacts, refetchInterval: 5000 });
  const runsQ = useQuery({ queryKey: ["contacts", "mandate-runs"], queryFn: getMandateRuns, refetchInterval: 5000 });
  const accountsQ = useQuery({ queryKey: ["receivables", "accounts"], queryFn: getAccounts, refetchInterval: 5000 });

  const [target, setTarget] = useState<"recurring" | "account">("recurring");
  const [rzpId, setRzpId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [phone, setPhone] = useState("");
  const [language, setLanguage] = useState<Language>(DEFAULT_LANGUAGE);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const save = useMutation({
    mutationFn: () =>
      setContact({
        ...(target === "recurring" ? { razorpay_customer_id: rzpId.trim() } : { business_account_id: accountId }),
        phone,
        preferred_language: language,
      }),
    onSuccess: () => {
      setMsg({ ok: true, text: "Saved. The customer verifies the number and gives consent on their own link." });
      setPhone("");
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: ["receivables"] });
    },
    onError: (e) => {
      const d = e instanceof ApiError ? (e.body as { detail?: unknown } | null)?.detail : null;
      const text = d && typeof d === "object" && "message" in d ? String((d as { message: unknown }).message)
        : typeof d === "string" ? d : "Could not save";
      setMsg({ ok: false, text });
    },
  });

  const canSave = phone.trim().length >= 10 && (target === "recurring" ? rzpId.trim() : accountId);

  return (
    <div>
      <h3 className="text-sm font-bold mb-2">Failed recurring debits</h3>
      {!runsQ.data || runsQ.data.length === 0 ? (
        <p className="text-xs text-ink/40 mb-5">None yet. Run <code>simulate_webhook.py pending</code> to create one.</p>
      ) : (
        <div className="mb-5 overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Subscription</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Amount</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead>Customer's page</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runsQ.data.map((r) => {
                const b = contactBadge(r.contact);
                return (
                  <TableRow key={r.id}>
                    <TableCell className="text-xs">{r.subscription_id}</TableCell>
                    <TableCell className="text-xs">{r.razorpay_customer_id ?? "—"}</TableCell>
                    <TableCell className="text-xs tabular-nums">₹{Math.round(r.amount_inr).toLocaleString("en-IN")}</TableCell>
                    <TableCell className="text-xs">{r.status} · {r.attempts_made} failed</TableCell>
                    <TableCell><ToneBadge label={b.label} tone={b.tone} /></TableCell>
                    <TableCell>
                      <button type="button" onClick={() => copyLink(r.self_serve_path)}
                              className="text-xs underline text-ink/60">
                        Copy link
                      </button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <h3 className="text-sm font-bold mb-1">Add or update a phone number</h3>
      <p className="text-[11.5px] text-ink/40 mb-3">
        You supply the number and language. The customer then confirms it with a code and agrees to calls themselves, on
        their own page - consent can't be entered here. Changing a number clears any earlier verification and consent.
      </p>
      <div className="flex flex-wrap gap-2 items-end mb-2">
        <select value={target} onChange={(e) => setTarget(e.target.value as "recurring" | "account")}
                className="rounded-md border border-ink/15 bg-white px-2 py-1.5 text-xs">
          <option value="recurring">Recurring-debit customer</option>
          <option value="account">B2B account</option>
        </select>
        {target === "recurring" ? (
          <input value={rzpId} onChange={(e) => setRzpId(e.target.value)} placeholder="Razorpay customer id (cust_…)"
                 className="rounded-md border border-ink/15 bg-white px-2 py-1.5 text-xs w-56" />
        ) : (
          <select value={accountId} onChange={(e) => setAccountId(e.target.value)}
                  className="rounded-md border border-ink/15 bg-white px-2 py-1.5 text-xs w-56">
            <option value="">Choose an account…</option>
            {accountsQ.data?.map((a) => <option key={a.id} value={a.id}>{a.company_name}</option>)}
          </select>
        )}
        <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="Mobile number"
               className="rounded-md border border-ink/15 bg-white px-2 py-1.5 text-xs w-36" inputMode="tel" />
        <select value={language} onChange={(e) => setLanguage(e.target.value as Language)}
                className="rounded-md border border-ink/15 bg-white px-2 py-1.5 text-xs">
          {(Object.keys(LANGUAGE_LABEL) as Language[]).map((l) => <option key={l} value={l}>{LANGUAGE_LABEL[l]}</option>)}
        </select>
        <button type="button" disabled={!canSave || save.isPending} onClick={() => save.mutate()}
                className="rounded-md px-3.5 py-1.5 text-xs font-bold bg-ink text-cream disabled:opacity-50">
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
      {msg && <p className={`text-xs mb-3 ${msg.ok ? "text-soft" : "text-failed"}`}>{msg.text}</p>}

      <h3 className="text-sm font-bold mt-5 mb-2">All contacts</h3>
      {!contactsQ.data || contactsQ.data.length === 0 ? (
        <p className="text-xs text-ink/40">No contacts yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Who</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead>Language</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {contactsQ.data.map((c) => {
                const b = contactBadge(c);
                const who = c.razorpay_customer_id ?? (c.business_account_id ? "B2B account" : c.customer_id ? "Storefront customer" : "—");
                return (
                  <TableRow key={c.id}>
                    <TableCell className="text-xs">{who}</TableCell>
                    <TableCell className="text-xs">{c.phone_masked || "—"}</TableCell>
                    <TableCell className="text-xs">{LANGUAGE_LABEL[c.preferred_language]}</TableCell>
                    <TableCell><ToneBadge label={b.label} tone={b.tone} /></TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

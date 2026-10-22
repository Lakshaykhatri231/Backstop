import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { bizPayLink, bizSendOtp, bizVerifyPhone, getBizOverview, saveBizPermissions } from "@/lib/api/account";

import { AccountShell, Card } from "./AccountShell";
import { errMessage, PermissionsCard } from "./PermissionsCard";

const inr = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;
const day = (iso: string) => new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleDateString("en-IN", { dateStyle: "medium", timeZone: "Asia/Kolkata" });

// A business customer's own page: what they owe, a Pay now button for each
// invoice, and the permissions we ask for. Payment never depends on them.
export function BusinessInvoicesPage() {
  return (
    <AccountShell role="business" title="Your invoices" subtitle="What you owe, and how you would like us to reach you if one runs late.">
      <Body />
    </AccountShell>
  );
}

function Body() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["biz", "overview"], queryFn: getBizOverview, refetchInterval: 15000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["biz", "overview"] });
  const pay = useMutation({
    mutationFn: async (id: string) => {
      const tab = window.open("", "_blank");
      try {
        const r = await bizPayLink(id);
        if (r.is_stub) { tab?.close(); throw new Error("Online payment is not available for this invoice right now. Please contact the merchant."); }
        if (tab) tab.location.href = r.url; else window.location.href = r.url;
        return r;
      } catch (e) { tab?.close(); throw e; }
    },
    onSuccess: refresh,
  });

  if (q.isLoading) return <p className="text-sm text-ink/50">Loading…</p>;
  if (q.isError || !q.data) return <p className="text-sm text-failed">Could not load your invoices.</p>;
  const { invoices, contact, notes, consent_text } = q.data;
  const owing = invoices.filter((i) => i.outstanding_inr > 0 && i.status !== "written_off");
  const total = owing.reduce((s, i) => s + i.outstanding_inr, 0);

  return (
    <>
      <Card>
        <p className="text-xs uppercase tracking-[0.15em] text-ink/50">{q.data.account.company_name}</p>
        <p className="font-display text-3xl font-semibold mt-1">{inr(total)} <span className="text-sm font-normal text-ink/50">outstanding</span></p>
        <ul className="mt-4 divide-y divide-ink/5">
          {invoices.length === 0 && <li className="py-3 text-sm text-ink/50">No invoices yet.</li>}
          {invoices.map((i) => {
            const open = i.outstanding_inr > 0 && !["paid", "written_off", "disputed"].includes(i.status);
            return (
              <li key={i.id} className="py-3 flex items-center gap-3">
                <div className="flex-1">
                  <p className="font-semibold text-sm">{i.invoice_no}</p>
                  <p className="text-xs text-ink/60">
                    Due {day(i.due_at)}{i.days_past_due > 0 && <span className="text-failed font-semibold"> · {i.days_past_due} days late</span>} · {i.status.replace("_", " ")}
                  </p>
                </div>
                <div className="text-right text-sm">
                  <p className="font-semibold">{inr(i.outstanding_inr)}</p>
                  {i.amount_paid > 0 && <p className="text-xs text-ink/50">of {inr(i.amount_inr)}</p>}
                </div>
                {open && (
                  <Button size="sm" disabled={pay.isPending} onClick={() => pay.mutate(i.id)}>Pay now</Button>
                )}
              </li>
            );
          })}
        </ul>
        {pay.isError && <p className="mt-3 text-sm text-failed">{errMessage(pay.error)}</p>}
      </Card>

      <Card>
        <h2 className="font-display text-lg font-semibold">Your permissions</h2>
        <div className="mt-3">
          <PermissionsCard
            contact={contact}
            consentText={consent_text}
            notes={notes}
            save={saveBizPermissions}
            sendOtp={bizSendOtp}
            verifyPhone={bizVerifyPhone}
            onChanged={refresh}
            intro="We text you first if an invoice runs late. If you allow it, we may also call between 08:00 and 19:00. You can still pay any time without allowing anything."
          />
        </div>
      </Card>
    </>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { listDuplicatePayments, refundDuplicate } from "@/lib/api/autopay";

// A basket paid twice (store checkout + the Razorpay link we sent for its
// failed payment). The second payment is never booked; a person refunds it.
export function DuplicatePayments() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["payment-links", "duplicates"], queryFn: listDuplicatePayments, refetchInterval: 15000 });
  const refund = useMutation({
    mutationFn: (d: { row: string; pid: string }) => refundDuplicate(d.row, d.pid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["payment-links", "duplicates"] }),
  });
  if (!q.data || q.data.length === 0) return null;
  return (
    <section className="mt-6 rounded-2xl border border-failed/30 bg-failed/5 p-4">
      <h3 className="font-display font-semibold text-failed">Paid twice: refund needed</h3>
      <p className="text-xs text-ink/60 mt-1">
        These customers paid a failed basket in the store AND through the payment link. The second payment was not
        counted as revenue. Refund it on Razorpay:
      </p>
      <ul className="mt-3 space-y-2 text-sm">
        {q.data.map((d) => (
          <li key={d.payment_id} className="flex items-center gap-3 flex-wrap">
            <code className="text-xs">{d.payment_id}</code>
            <span>₹{Math.round(d.amount_inr).toLocaleString("en-IN")}</span>
            <button type="button" disabled={refund.isPending}
              onClick={() => refund.mutate({ row: d.payment_link_row, pid: d.payment_id })}
              className="ml-auto px-3 py-1 rounded-md border border-failed/40 text-failed text-xs disabled:opacity-50">
              Refund
            </button>
          </li>
        ))}
      </ul>
      {refund.isError && <p className="text-xs text-failed mt-2">{String(refund.error)}</p>}
    </section>
  );
}

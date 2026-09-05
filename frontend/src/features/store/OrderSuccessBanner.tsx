import type { OrderSuccessState } from "@/lib/hooks/useStorefront";

export function OrderSuccessBanner({ order, onDismiss }: { order: OrderSuccessState | null; onDismiss: () => void }) {
  if (!order) return null;

  return (
    <div className="max-w-[640px] mx-auto mt-5 p-4 rounded-xl bg-soft/10 border border-soft/40">
      <div className="text-center">
        <p className="text-sm text-ink mb-3">
          ✅ Order placed — ₹{order.amountPaidInr.toLocaleString("en-IN")} charged.
          {order.savedInr > 0 && (
            <span className="block text-soft font-semibold mt-1">
              You saved ₹{order.savedInr.toLocaleString("en-IN")} with this offer.
            </span>
          )}
        </p>
        <button onClick={onDismiss} className="px-6 py-2 rounded-lg bg-soft text-white text-sm font-semibold">
          Okay
        </button>
      </div>
    </div>
  );
}

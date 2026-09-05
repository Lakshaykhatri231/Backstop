import type { TimeoutBannerState } from "@/lib/hooks/useStorefront";

export function TimeoutBanner({ banner, onDismiss }: { banner: TimeoutBannerState | null; onDismiss: () => void }) {
  if (!banner) return null;
  const hasIncentive = banner.incentive_pct != null;

  return (
    <div className={`rounded-lg border p-3 mb-3 text-xs ${hasIncentive ? "bg-soft/10 border-soft/40" : "bg-brand/10 border-brand/30"}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="text-ink">{banner.message}</div>
        <button onClick={onDismiss} className="text-ink/40 hover:text-ink text-base leading-none">
          ×
        </button>
      </div>
      {banner.items.length > 0 && (
        <div className="mt-2 text-ink/50 text-[11px]">
          {banner.items.map((i) => `${i.name} × ${i.qty}`).join(", ")}
        </div>
      )}
      {hasIncentive && banner.final_amount_inr != null && (
        <div className="mt-1.5 text-soft font-semibold">
          ₹{banner.original_amount_inr.toLocaleString("en-IN")} → ₹{banner.final_amount_inr.toLocaleString("en-IN")}{" "}
          ({banner.incentive_pct}% off)
          <div className="text-[10.5px] text-ink/40 font-normal mt-1">
            Applied automatically at checkout — the offer follows your cart as you edit it, as long as the total
            stays under your offer limit.
          </div>
        </div>
      )}
    </div>
  );
}

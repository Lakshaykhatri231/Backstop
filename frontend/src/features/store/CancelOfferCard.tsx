import type { CancelOffer, CatalogItem } from "@/lib/api/storefront";
import { ProductImage } from "./ProductImage";

export function CancelOfferCard({
  offer,
  catalog,
  busy,
  onResume,
  onDecline,
}: {
  offer: CancelOffer | null;
  catalog: CatalogItem[];
  busy: boolean;
  onResume: () => void;
  onDecline: () => void;
}) {
  if (!offer) return null;
  const hasIncentive = offer.incentive_pct != null && offer.final_amount_inr != null;

  return (
    <div
      className={`rounded-2xl bg-white p-5 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-4 border ${
        hasIncentive ? "border-soft/50" : "border-ink/5"
      }`}
    >
      <p className="font-display font-semibold text-sm mb-1">Changed your mind?</p>
      <p className="text-xs text-ink/50 mb-3">We saved what you had — here's exactly what's waiting for you.</p>
      {offer.items.map((item) => {
        const product = catalog.find((p) => p.id === item.sku);
        return (
          <div key={item.sku} className="flex items-center gap-2.5 text-xs text-ink/60 mb-1.5">
            <ProductImage sku={item.sku} alt={product?.name ?? item.sku} className="size-8 shrink-0 rounded-md" />
            <span className="truncate">
              {product ? product.name : item.sku} × {item.qty}
            </span>
          </div>
        );
      })}
      <div className="border-t border-ink/10 mt-2 pt-2">
        {hasIncentive && offer.final_amount_inr != null ? (
          <>
            <div className="flex justify-between text-xs text-ink/60">
              <span>Total</span>
              <span>₹{offer.original_amount_inr.toLocaleString("en-IN")}</span>
            </div>
            <div className="flex justify-between text-xs text-soft">
              <span>Off ({offer.incentive_pct}%)</span>
              <span>−₹{(offer.original_amount_inr - offer.final_amount_inr).toLocaleString("en-IN")}</span>
            </div>
            <div className="flex justify-between text-sm font-bold mt-1">
              <span>You'd pay</span>
              <span className="text-soft">₹{offer.final_amount_inr.toLocaleString("en-IN")}</span>
            </div>
            <p className="text-[10.5px] text-ink/40 mt-2">
              The discount follows your cart — add or remove items freely; it stays valid while the total is within
              your offer limit.
            </p>
          </>
        ) : (
          <div className="flex justify-between text-sm font-bold">
            <span>Total</span>
            <span>₹{offer.original_amount_inr.toLocaleString("en-IN")}</span>
          </div>
        )}
      </div>
      <div className="flex gap-2 mt-3">
        <button
          onClick={onResume}
          disabled={busy}
          className={`flex-1 rounded-lg text-sm font-semibold py-2 text-white disabled:opacity-50 ${
            hasIncentive ? "bg-soft" : "bg-ink"
          }`}
        >
          {hasIncentive ? "Add back to cart (with discount)" : "Resume cart"}
        </button>
        <button
          onClick={onDecline}
          disabled={busy}
          className="flex-1 rounded-lg border border-ink/15 text-ink/60 text-sm font-medium py-2 disabled:opacity-50"
        >
          No thanks
        </button>
      </div>
    </div>
  );
}

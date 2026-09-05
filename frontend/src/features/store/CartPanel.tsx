import { TimeoutBanner } from "./TimeoutBanner";
import { ProductImage } from "./ProductImage";
import type { Cart } from "@/lib/api/storefront";
import type { CatalogItem } from "@/lib/api/storefront";
import type { TimeoutBannerState } from "@/lib/hooks/useStorefront";

export function CartPanel({
  cart,
  catalog,
  timeoutBanner,
  onDismissTimeoutBanner,
  onRemove,
  onCancelCart,
  onCheckout,
  busy,
  checkoutError,
}: {
  cart: Cart;
  catalog: CatalogItem[];
  timeoutBanner: TimeoutBannerState | null;
  onDismissTimeoutBanner: () => void;
  onRemove: (sku: string) => void;
  onCancelCart: () => void;
  onCheckout: () => void;
  busy: boolean;
  checkoutError: string;
}) {
  const offer = cart.active_offer;

  return (
    <div className="rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-4">
      <p className="font-display font-semibold text-sm mb-3">Cart</p>
      <TimeoutBanner banner={timeoutBanner} onDismiss={onDismissTimeoutBanner} />

      {cart.items.length === 0 && <p className="text-sm text-ink/40">Empty</p>}
      {cart.items.map((item) => {
        const product = catalog.find((p) => p.id === item.sku);
        return (
          <div key={item.sku} className="flex items-center justify-between text-sm mb-2 gap-2">
            <div className="flex items-center gap-2.5 min-w-0">
              <ProductImage sku={item.sku} alt={product?.name ?? item.sku} className="size-9 shrink-0 rounded-lg" />
              <span className="truncate">
                {product ? product.name : item.sku} × {item.qty}
              </span>
            </div>
            <button onClick={() => onRemove(item.sku)} className="text-failed hover:underline shrink-0">
              remove
            </button>
          </div>
        );
      })}

      {cart.items.length > 0 && (
        <>
          <div
            className={`flex justify-between border-t border-ink/10 mt-2 pt-2 text-sm ${
              offer ? "font-medium text-ink/60" : "font-semibold text-ink"
            }`}
          >
            <span>Total</span>
            <span className="tabular-nums">₹{cart.amount_inr.toLocaleString("en-IN")}</span>
          </div>

          {offer && offer.within_cap && offer.discounted_amount_inr != null && (
            <>
              <div className="flex justify-between text-sm text-soft mt-1">
                <span>Offer ({offer.incentive_pct}% off)</span>
                <span className="tabular-nums">
                  −₹{(cart.amount_inr - offer.discounted_amount_inr).toLocaleString("en-IN")}
                </span>
              </div>
              <div className="flex justify-between text-base font-bold mt-1">
                <span>You pay</span>
                <span className="text-soft tabular-nums">₹{offer.discounted_amount_inr.toLocaleString("en-IN")}</span>
              </div>
            </>
          )}

          {offer && !offer.within_cap && (
            <div className="text-xs text-declined mt-2 leading-relaxed">
              ⏸ Your {offer.incentive_pct}% offer is paused — it's valid on carts up to ₹
              {offer.amount_cap_inr.toLocaleString("en-IN")}. Remove ₹
              {(cart.amount_inr - offer.amount_cap_inr).toLocaleString("en-IN")} worth of items to reactivate it.
            </div>
          )}

          {offer && (
            <div className="flex items-center justify-between mt-2 gap-2">
              <span className="text-[10.5px] text-ink/40">
                Offer valid on carts up to ₹{offer.amount_cap_inr.toLocaleString("en-IN")} — it follows your cart as
                you edit it.
              </span>
              <button
                onClick={onCancelCart}
                className="text-[10.5px] text-failed underline whitespace-nowrap"
              >
                Cancel cart
              </button>
            </div>
          )}

          <button
            onClick={onCheckout}
            disabled={busy}
            className="w-full mt-3 rounded-lg bg-ink text-cream font-semibold py-2.5 text-sm disabled:opacity-50"
          >
            Pay with Razorpay
          </button>
          {checkoutError && <p className="text-xs text-failed mt-2">{checkoutError}</p>}
        </>
      )}
    </div>
  );
}

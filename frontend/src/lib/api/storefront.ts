import { apiFetch } from "./client";

export type CatalogItem = {
  id: string;
  name: string;
  price_inr: number;
};

export type CartItem = {
  sku: string;
  qty: number;
};

export type ActiveOffer = {
  cart_event_id: string;
  incentive_pct: number;
  amount_cap_inr: number;
  within_cap: boolean;
  full_amount_inr: number;
  discounted_amount_inr: number | null;
};

export type Cart = {
  items: CartItem[];
  amount_inr: number;
  active_offer: ActiveOffer | null;
};

// Field set genuinely varies between the "fresh decision" and "deduplicated"
// branches server-side — treat every field beyond cart_event_id as optional.
export type RecoveryDecision = {
  cart_event_id: string | null;
  action: string | null;
  confidence: number | null;
  reasoning: string;
  outcome?: string | null;
  tier: string;
  status: string | null;
  items: CartItem[];
  original_amount_inr: number;
  incentive_pct: number | null;
  final_amount_inr: number | null;
  deduplicated?: boolean;
  note?: string;
};

export type CancelOffer = {
  cart_event_id: string;
  items: CartItem[];
  original_amount_inr: number;
  incentive_pct: number | null;
  final_amount_inr: number | null;
  action: string | null;
};

export type PaymentFailureNotice = {
  action: string;
  failure_reason: string | null;
};

export type PendingSignals = {
  cancel_offer: CancelOffer | null;
  payment_failure_notice: PaymentFailureNotice | null;
};

export type CheckoutOrder = {
  razorpay_order_id: string;
  razorpay_key_id: string;
  amount_paise: number;
  currency: string;
  customer_name: string;
  customer_email: string;
  local_order_id: string;
};

export type VerifyPaymentResult = {
  status: string;
  already_processed: boolean;
  revenue_booked: boolean;
  order_id: string;
};

export function getCatalog() {
  return apiFetch<CatalogItem[]>("/catalog", { auth: false });
}

export function getCart() {
  return apiFetch<Cart>("/cart");
}

export function addToCart(sku: string, qty = 1) {
  return apiFetch<Cart>("/cart/add", { method: "POST", body: { sku, qty } });
}

export function removeFromCart(sku: string) {
  return apiFetch<Cart>("/cart/remove", { method: "POST", body: { sku } });
}

export function deleteCart() {
  return apiFetch<{ status: string; recovery_decision?: RecoveryDecision }>("/cart", { method: "DELETE" });
}

export function getPendingSignals() {
  return apiFetch<PendingSignals>("/cart/pending-signals");
}

export function resumeCart(cartEventId: string) {
  return apiFetch<Cart>("/cart/resume", { method: "POST", body: { cart_event_id: cartEventId } });
}

export function declineResume(cartEventId: string) {
  return apiFetch<{ status: string }>("/cart/decline-resume", {
    method: "POST",
    body: { cart_event_id: cartEventId },
  });
}

export function checkout() {
  return apiFetch<CheckoutOrder>("/checkout", { method: "POST" });
}

export function verifyCheckout(
  razorpay_order_id: string,
  razorpay_payment_id: string,
  razorpay_signature: string,
) {
  return apiFetch<VerifyPaymentResult>("/checkout/verify", {
    method: "POST",
    body: { razorpay_order_id, razorpay_payment_id, razorpay_signature },
  });
}

export function giveUpFailed() {
  return apiFetch<{ settled_runs: { razorpay_order_id: string; amount_inr: number }[]; cart_cleared: boolean }>(
    "/checkout/give-up-failed",
    { method: "POST" },
  );
}

// Demo/tutorial control, but a real customer-authenticated storefront action —
// this is the ONLY way the silent-abandon signal ever fires in this system
// (there is no real client-side inactivity timer anywhere).
export function simulateCartTimeout() {
  return apiFetch<{ status: string; recovery_decision: RecoveryDecision }>("/debug/simulate-cart-timeout", {
    method: "POST",
  });
}

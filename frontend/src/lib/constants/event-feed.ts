import type { Tone } from "./tone";
import type { OutcomeEvent } from "@/lib/api/outcomes";
import type { MerchantOrder, MerchantCartEvent } from "@/lib/api/merchant";

export type EventTypeBadge = { label: string; tone: Tone };

// Ported verbatim from static/index.html's eventTypeBadge(). Three shapes:
// Razorpay-reported payment failures, the drop-off poller's abandonments,
// and the one customer-initiated exception (give-up on a failed payment).
export function eventTypeBadge(eventType: string): EventTypeBadge {
  if (eventType === "checkout.abandoned" || eventType === "checkout_abandoned") {
    return { label: "Drop-off", tone: "indigo" };
  }
  if (eventType === "payment_failure_given_up") {
    return { label: "Give Up", tone: "neutral" };
  }
  return { label: "Failure", tone: "failed" };
}

export type FeedItem =
  | { kind: "payment"; ts: string; order: MerchantOrder }
  | { kind: "event"; ts: string; event: OutcomeEvent }
  | { kind: "cart"; ts: string; cart: MerchantCartEvent };

// Ported verbatim from static/index.html's mergeFeed(). Successful payments
// come from /merchant/orders (the storefront ledger), NOT /outcomes/events —
// the Event table only ever records revenue-LOSS signals (failures,
// drop-offs), so a captured payment has no Event row by design. Pre-checkout
// cart events (silent abandon / explicit cancel) live in their own table too
// since no Razorpay order exists yet for them — third source, same feed.
export function mergeFeed(
  events: OutcomeEvent[],
  orders: MerchantOrder[],
  cartEvents: MerchantCartEvent[],
): FeedItem[] {
  const payments: FeedItem[] = (orders || [])
    .filter((o) => o.status === "captured")
    .map((o) => ({ kind: "payment", ts: o.resolved_at || o.created_at, order: o }));
  const evts: FeedItem[] = (events || []).map((e) => ({ kind: "event", ts: e.received_at, event: e }));
  const carts: FeedItem[] = (cartEvents || []).map((c) => ({ kind: "cart", ts: c.created_at, cart: c }));
  return [...payments, ...evts, ...carts].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
}

import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import * as api from "../api/storefront";
import { failureNoticeCopy, timeoutNudgeCopy } from "../copy";
import { useCurrentCustomer } from "./useAuth";

export type TimeoutBannerState = {
  message: string;
  items: { name: string; qty: number }[];
  incentive_pct: number | null;
  original_amount_inr: number;
  final_amount_inr: number | null;
};

export type FailureNoticeState = {
  message: string;
  action: string;
};

export type OrderSuccessState = {
  amountPaidInr: number;
  savedInr: number;
};

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong";
}

// Mirrors the old static/store.html single-component Store, split into a
// hook so presentation stays in dumb components. Server state (catalog,
// cart) is TanStack Query; the banners below are ephemeral, action-triggered
// UI state that doesn't map onto any single query.
export function useStorefront() {
  const queryClient = useQueryClient();
  const me = useCurrentCustomer();

  const catalogQuery = useQuery({
    queryKey: ["catalog"],
    queryFn: api.getCatalog,
    enabled: !!me.data,
  });
  const cartQuery = useQuery({
    queryKey: ["cart"],
    queryFn: api.getCart,
    enabled: !!me.data,
  });

  const [busy, setBusy] = useState(false);
  const [checkoutError, setCheckoutError] = useState("");
  const [timeoutBanner, setTimeoutBanner] = useState<TimeoutBannerState | null>(null);
  const [cancelOffer, setCancelOffer] = useState<api.CancelOffer | null>(null);
  const [failureNotice, setFailureNotice] = useState<FailureNoticeState | null>(null);
  const [orderSuccess, setOrderSuccess] = useState<OrderSuccessState | null>(null);
  const [agentNotice, setAgentNotice] = useState<string | null>(null);

  const catalog = catalogQuery.data ?? [];
  const cart = cartQuery.data ?? { items: [], amount_inr: 0, active_offer: null };

  const fetchPendingSignals = useCallback(async () => {
    try {
      const r = await api.getPendingSignals();
      setCancelOffer(r.cancel_offer);
      if (r.payment_failure_notice) {
        setFailureNotice({
          message: failureNoticeCopy(r.payment_failure_notice.failure_reason, r.payment_failure_notice.action),
          action: r.payment_failure_notice.action,
        });
        return true;
      }
      return false;
    } catch {
      return false; // purely additive UI — a failed poll shouldn't surface an error
    }
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["auth", "me"] }),
      queryClient.invalidateQueries({ queryKey: ["catalog"] }),
      queryClient.invalidateQueries({ queryKey: ["cart"] }),
    ]);
    await fetchPendingSignals();
  }, [queryClient, fetchPendingSignals]);

  // One-time load parity with the old app's refresh-on-mount, once we know
  // who's logged in.
  useEffect(() => {
    if (me.data) fetchPendingSignals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me.data?.id]);

  function pollForFailureNotice() {
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts += 1;
      const found = await fetchPendingSignals();
      if (found) {
        setCheckoutError("");
        clearInterval(interval);
      } else if (attempts >= 8) {
        setCheckoutError("That payment didn't go through — try again, or use a different card.");
        clearInterval(interval);
      }
    }, 2000);
  }

  async function addToCart(sku: string) {
    const updated = await api.addToCart(sku, 1);
    queryClient.setQueryData(["cart"], updated);
  }

  async function removeFromCart(sku: string) {
    const updated = await api.removeFromCart(sku);
    queryClient.setQueryData(["cart"], updated);
  }

  async function simulateTimeout() {
    setBusy(true);
    setTimeoutBanner(null);
    try {
      const r = await api.simulateCartTimeout();
      const d = r.recovery_decision;
      // Only actions with real customer-facing copy get a banner — a
      // risk-tier reminder still gets one (flat, no discount), but
      // no_action never does.
      if (d.action !== "no_action") {
        const namedItems = (d.items || []).map((i) => {
          const p = catalog.find((c) => c.id === i.sku);
          return { name: p ? p.name : i.sku, qty: i.qty };
        });
        setTimeoutBanner({
          message: timeoutNudgeCopy(d.tier, namedItems.map((i) => i.name)),
          items: namedItems,
          incentive_pct: d.incentive_pct,
          original_amount_inr: d.original_amount_inr,
          final_amount_inr: d.final_amount_inr,
        });
      }
      await refresh();
    } catch (e) {
      setCheckoutError(errorMessage(e));
    }
    setBusy(false);
  }

  async function deleteCart() {
    setBusy(true);
    setTimeoutBanner(null);
    try {
      const r = await api.deleteCart();
      const d = r.recovery_decision;
      if (d) {
        if (d.deduplicated) {
          // This basket already has an open failed-payment run.
          setAgentNotice(d.reasoning);
        } else if (d.action === "escalate_to_human") {
          setAgentNotice(
            "The recovery agent chose not to make an offer this time — repeated cancellations are handed to a human reviewer instead of chasing with more automated win-backs. No resume card was created.",
          );
        } else if (d.action === "no_action") {
          setAgentNotice(
            "The recovery agent chose not to chase this cancellation (low-signal tier/history) — no resume card was created.",
          );
        }
        if (d.status === "pending" && d.cart_event_id) {
          setCancelOffer({
            cart_event_id: d.cart_event_id,
            items: d.items,
            original_amount_inr: d.original_amount_inr,
            incentive_pct: d.incentive_pct,
            final_amount_inr: d.final_amount_inr,
            action: d.action,
          });
        }
      }
      await refresh();
    } catch (e) {
      setCheckoutError(errorMessage(e));
    }
    setBusy(false);
  }

  async function resumeCancelledCart() {
    if (!cancelOffer) return;
    setBusy(true);
    try {
      const updated = await api.resumeCart(cancelOffer.cart_event_id);
      queryClient.setQueryData(["cart"], updated);
      setCancelOffer(null);
    } catch (e) {
      setCheckoutError(errorMessage(e));
    }
    setBusy(false);
  }

  async function declineCancelOffer() {
    if (!cancelOffer) return;
    try {
      await api.declineResume(cancelOffer.cart_event_id);
    } catch {
      // no-op
    }
    setCancelOffer(null);
  }

  async function giveUpFailedOrder() {
    setBusy(true);
    try {
      await api.giveUpFailed();
      setFailureNotice(null);
      setTimeoutBanner(null);
      await refresh();
    } catch (e) {
      setCheckoutError(errorMessage(e));
    }
    setBusy(false);
  }

  function openRazorpayCheckout(order: api.CheckoutOrder, cartAmountAtCheckout: number) {
    const Razorpay = window.Razorpay;
    if (!Razorpay) {
      setCheckoutError("Payment widget failed to load — check your connection and try again.");
      setBusy(false);
      return;
    }
    const rzp = new Razorpay({
      key: order.razorpay_key_id,
      amount: order.amount_paise,
      currency: order.currency,
      name: "Backstop Store",
      description: "Demo checkout",
      order_id: order.razorpay_order_id,
      prefill: { name: order.customer_name, email: order.customer_email },
      theme: { color: "#ea580c" },
      handler: async (response) => {
        try {
          await api.verifyCheckout(
            response.razorpay_order_id,
            response.razorpay_payment_id,
            response.razorpay_signature,
          );
          // timeoutBanner is a frozen client-side snapshot taken when
          // "Simulate cart timeout" was clicked — nothing else clears it.
          setTimeoutBanner(null);
          const amountPaidInr = order.amount_paise / 100;
          const savedInr = Math.max(0, Math.round((cartAmountAtCheckout - amountPaidInr) * 100) / 100);
          setOrderSuccess({ amountPaidInr, savedInr });
        } catch (e) {
          setCheckoutError("Payment could not be verified: " + errorMessage(e));
        }
        await refresh();
      },
      modal: { ondismiss: () => refresh() },
    });
    rzp.on("payment.failed", (response) => {
      // Client-side signal only, deliberately generic — the real classified
      // message comes from the server-to-server webhook a few seconds
      // later, surfaced via pollForFailureNotice. Never show Razorpay's raw
      // error.description directly.
      console.error("Razorpay payment failed", response.error);
      setCheckoutError("That payment didn't go through. Checking what happened...");
      setTimeoutBanner(null);
      refresh();
      pollForFailureNotice();
    });
    rzp.open();
    setBusy(false);
  }

  async function checkout() {
    setCheckoutError("");
    setFailureNotice(null);
    setBusy(true);
    // Captured before the Razorpay modal opens — not re-read from live cart
    // state inside the success handler, since the modal can stay open a
    // while and local state could drift in the meantime.
    const cartAmountAtCheckout = cart.amount_inr;
    try {
      const order = await api.checkout();
      openRazorpayCheckout(order, cartAmountAtCheckout);
    } catch (e) {
      setCheckoutError(errorMessage(e));
      setBusy(false);
    }
  }

  return {
    me: me.data ?? null,
    meLoading: me.isLoading,
    catalog,
    cart,
    busy,
    checkoutError,
    timeoutBanner,
    cancelOffer,
    failureNotice,
    orderSuccess,
    agentNotice,
    addToCart,
    removeFromCart,
    simulateTimeout,
    deleteCart,
    resumeCancelledCart,
    declineCancelOffer,
    giveUpFailedOrder,
    checkout,
    dismissTimeoutBanner: () => setTimeoutBanner(null),
    dismissFailureNotice: () => setFailureNotice(null),
    dismissOrderSuccess: () => setOrderSuccess(null),
    dismissAgentNotice: () => setAgentNotice(null),
  };
}

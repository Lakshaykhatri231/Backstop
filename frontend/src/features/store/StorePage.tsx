import { AuthCard } from "@/features/auth/AuthCard";
import { useLogout } from "@/lib/hooks/useAuth";
import { useStorefront } from "@/lib/hooks/useStorefront";

import { StoreHeader } from "./StoreHeader";
import { AccountStatsCard } from "./AccountStatsCard";
import { CatalogGrid } from "./CatalogGrid";
import { CartPanel } from "./CartPanel";
import { CancelOfferCard } from "./CancelOfferCard";
import { FailureRetryBanner } from "./FailureRetryBanner";
import { OrderSuccessBanner } from "./OrderSuccessBanner";
import { AgentNoticeBanner } from "./AgentNoticeBanner";
import { DemoTutorialCard } from "./DemoTutorialCard";

export function StorePage() {
  const store = useStorefront();
  const logout = useLogout();

  if (!store.me) {
    if (store.meLoading) {
      return <div className="min-h-screen grid place-items-center text-ink/40 bg-cream">Loading…</div>;
    }
    return (
      <div className="relative min-h-screen w-full overflow-hidden text-cream font-body">
        <div className="absolute inset-0 -z-10 bg-gradient-to-br from-[#3a1f0a] via-[#7c3f12] to-[#d97706]" />
        <div className="absolute -top-24 -right-24 -z-10 size-96 rounded-full bg-amber-300/40 blur-3xl" />
        <div className="mx-auto max-w-[1200px] px-6 flex min-h-screen items-center">
          <div className="w-full max-w-md">
            <div className="flex items-center gap-2.5 mb-8">
              <span className="font-display font-semibold text-xl tracking-tight">Backstop Store</span>
            </div>
            <h1 className="font-display text-3xl font-semibold tracking-tight">Sign in to shop</h1>
            <p className="text-cream/70 text-sm mt-2">Demo storefront — real Razorpay test-mode checkout.</p>
            <AuthCard className="mt-8" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <StoreHeader customer={store.me} onLogout={logout} />

      <FailureRetryBanner
        notice={store.failureNotice}
        busy={store.busy}
        onRetry={store.checkout}
        onDismiss={store.dismissFailureNotice}
      />
      <AgentNoticeBanner notice={store.agentNotice} onDismiss={store.dismissAgentNotice} />
      <OrderSuccessBanner order={store.orderSuccess} onDismiss={store.dismissOrderSuccess} />

      <div className="mx-auto max-w-[1320px] px-6 py-8 grid lg:grid-cols-[1fr_380px] gap-6">
        <div>
          <AccountStatsCard stats={store.me.stats} />
          <DemoTutorialCard
            cartEmpty={store.cart.items.length === 0}
            busy={store.busy}
            hasFailureNotice={!!store.failureNotice}
            onSimulateTimeout={store.simulateTimeout}
            onDeleteCart={store.deleteCart}
            onGiveUp={store.giveUpFailedOrder}
          />
          <CatalogGrid catalog={store.catalog} onAdd={store.addToCart} />
        </div>

        <div>
          <CartPanel
            cart={store.cart}
            catalog={store.catalog}
            timeoutBanner={store.timeoutBanner}
            onDismissTimeoutBanner={store.dismissTimeoutBanner}
            onRemove={store.removeFromCart}
            onCancelCart={store.deleteCart}
            onCheckout={store.checkout}
            busy={store.busy}
            checkoutError={store.checkoutError}
          />

          <CancelOfferCard
            offer={store.cancelOffer}
            catalog={store.catalog}
            busy={store.busy}
            onResume={store.resumeCancelledCart}
            onDecline={store.declineCancelOffer}
          />
        </div>
      </div>
    </div>
  );
}

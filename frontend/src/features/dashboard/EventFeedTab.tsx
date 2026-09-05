import { mergeFeed } from "@/lib/constants/event-feed";
import { useCartEvents, useEvents, useOrders } from "@/lib/hooks/useDashboardData";

import { EventFeedTable } from "./components/EventFeedTable";

export function EventFeedTab() {
  const eventsQ = useEvents(50);
  const ordersQ = useOrders();
  const cartEventsQ = useCartEvents();

  if (!eventsQ.data || !ordersQ.data || !cartEventsQ.data) {
    return <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>;
  }

  const feed = mergeFeed(eventsQ.data, ordersQ.data, cartEventsQ.data);

  return (
    <div>
      <h1 className="font-display text-2xl font-semibold tracking-tight mb-4">Event Feed</h1>
      <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
        <EventFeedTable feed={feed} />
      </div>
    </div>
  );
}

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { actionLabel, actionTone } from "@/lib/constants/actions";
import { CART_EVENT_LABEL, CART_EVENT_TONE } from "@/lib/constants/cart-events";
import { tierTone } from "@/lib/constants/tiers";
import type { MerchantCartEvent } from "@/lib/api/merchant";
import { ToneBadge } from "./ToneBadge";

export function CartEventsTable({ cartEvents }: { cartEvents: MerchantCartEvent[] }) {
  if (cartEvents.length === 0) {
    return <div className="text-center py-8 text-ink/40 text-sm">No cart events yet — try the demo buttons on /store.</div>;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Customer</TableHead>
          <TableHead>Type</TableHead>
          <TableHead>Tier at time</TableHead>
          <TableHead>Amount</TableHead>
          <TableHead>Action</TableHead>
          <TableHead>Confidence</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {cartEvents.map((e) => (
          <TableRow key={e.id}>
            <TableCell className="text-xs">{e.customer_name || e.customer_id}</TableCell>
            <TableCell>
              <ToneBadge label={CART_EVENT_LABEL[e.event_type] ?? e.event_type} tone={CART_EVENT_TONE[e.event_type] ?? "neutral"} />
            </TableCell>
            <TableCell>
              <ToneBadge label={e.tier_at_time} tone={tierTone(e.tier_at_time)} />
            </TableCell>
            <TableCell className="text-xs tabular-nums">₹{Math.round(e.amount_inr)}</TableCell>
            <TableCell>
              <ToneBadge label={actionLabel(e.action)} tone={actionTone(e.action)} />
            </TableCell>
            <TableCell className="text-xs text-ink/40">{e.confidence ?? "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

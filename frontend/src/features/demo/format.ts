// Backend timestamps are naive UTC ISO strings ("2026-10-13T04:30:00"), so a
// missing zone must be read as UTC, then shown in IST (the contact window and
// every date the customer sees are IST).
export function parseUtc(iso: string): Date {
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

export function fmtIst(iso: string | null | undefined): string {
  if (!iso) return "—";
  return parseUtc(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export const inr = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;

export const TEMPLATE_LABEL: Record<string, string> = {
  otp: "Verification code",
  pre_debit_notice: "Pre-debit notice",
  payment_link: "Payment link",
  remandate_link: "Re-mandate link",
  promise_reminder: "Promise reminder",
  invoice_courtesy: "Invoice reminder",
  invoice_overdue: "Overdue notice",
  payment_link_offer: "Payment link (offer)",
  order_offer_ended: "Offer ended",
};

export const KIND_LABEL: Record<string, string> = {
  mandate_retry: "Retry the debit",
  pre_debit_notice: "Send pre-debit notice",
  promise_reminder: "Remind about promise",
  promise_check: "Check promise kept",
  invoice_chase: "Chase invoice",
  voice_call: "Place recovery call",
  order_link_reminder: "Send the order link reminder",
  mandate_pdn: "Create debit + bank notice",
  mandate_debit: "Debit the card mandate",
};

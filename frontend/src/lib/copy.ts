// Customer-facing copy for each decided action. Ported verbatim from
// static/store.html — deliberately never says more than what the backend
// actually decided (no invented urgency, no discount language unless the
// action is genuinely offer_incentive).

export function timeoutNudgeCopy(tier: string, itemNames: string[]): string {
  const item = itemNames[0] || "your item";
  switch (tier) {
    case "new":
      return "Still thinking it over? Your cart's saved whenever you're ready.";
    case "casual":
    case "regular":
      return itemNames.length > 1
        ? `Your ${itemNames[0]} and ${itemNames.length - 1} other item${itemNames.length > 2 ? "s" : ""} are still waiting in your cart.`
        : `Your ${item} is still waiting in your cart.`;
    case "loyal":
      return "You've been one of our best customers — here's a little something to finish this one.";
    case "risk":
      return "Your cart is still available if you'd like to complete your order.";
    default:
      return "Your cart is still here whenever you're ready.";
  }
}

export function failureNoticeCopy(failureReason: string | null, action: string): string {
  if (action === "escalate_to_human") {
    return "Having trouble completing your payment? Contact support and we'll help sort it out.";
  }
  if (action === "retry_now") {
    if (failureReason === "invalid_card") {
      return "That card wasn't accepted — try a different one and we'll retry.";
    }
    if (failureReason === "authentication_failed") {
      return "The verification code didn't go through — want to try again?";
    }
    if (failureReason === "network_error") {
      return "A connection hiccup interrupted your last payment — want to try again?";
    }
    return "Your last payment didn't go through — want to try again?";
  }
  if (action === "retry_later") {
    if (failureReason === "insufficient_funds") {
      return "Your last payment didn't go through. Give it a little time, then try again.";
    }
    return "That didn't go through again — worth giving it a little time before trying once more.";
  }
  // send_nudge — deliberately no retry button
  if (failureReason === "card_expired") {
    return "That card seems to have expired — try a different one.";
  }
  if (failureReason === "cancelled") {
    return "Still want to complete this order? Your cart is ready whenever you are.";
  }
  return "Your last payment didn't go through.";
}

export const TIER_LABEL: Record<string, string> = {
  new: "New",
  casual: "Casual",
  regular: "Regular",
  loyal: "Loyal",
  risk: "Risk",
};

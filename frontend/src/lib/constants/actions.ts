import type { Tone } from "./tone";

// Ported verbatim from static/index.html's ACTION_COLOR/ACTION_LABEL.
export const ACTION_TONE: Record<string, Tone> = {
  retry_now: "soft",
  retry_later: "loyal",
  send_nudge: "loyal",
  send_reminder: "loyal",
  send_resume_link: "indigo",
  offer_incentive: "indigo",
  escalate_to_human: "declined",
  no_action: "neutral",
  rule_default_fallback: "failed",
};

export const ACTION_LABEL: Record<string, string> = {
  retry_now: "Retry Now",
  retry_later: "Retry Later",
  send_nudge: "Card Nudge",
  send_reminder: "Reminder",
  send_resume_link: "Resume Link",
  offer_incentive: "Incentive",
  escalate_to_human: "Escalated",
  no_action: "No Action",
  rule_default_fallback: "Fallback",
};

export function actionLabel(action: string | null | undefined): string {
  if (!action) return "—";
  return ACTION_LABEL[action] ?? action;
}

export function actionTone(action: string | null | undefined): Tone {
  if (!action) return "neutral";
  return ACTION_TONE[action] ?? "neutral";
}

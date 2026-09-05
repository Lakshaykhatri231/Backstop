import type { Tone } from "./tone";

// Ported verbatim from static/index.html's OUTCOME_COLOR/outcomeColor().
const OUTCOME_TONE: Record<string, Tone> = {
  recovered: "soft",
  nudge_sent: "loyal",
  reminder_sent: "loyal",
  resume_link_sent: "indigo",
  escalated: "declined",
  no_action_taken: "neutral",
  customer_gave_up: "failed",
};

export function outcomeTone(outcome: string | null | undefined): Tone {
  if (!outcome) return "neutral";
  if (outcome.startsWith("pending_retry")) return "loyal";
  if (outcome.startsWith("incentive_offered")) return "indigo";
  return OUTCOME_TONE[outcome] ?? "neutral";
}

// Incentive Analysis "patterns" — plain-English observations from
// app/insights.py::generate_patterns, ranked opportunity > warning >
// positive > info by the backend already.
export const PATTERN_ICON: Record<string, string> = {
  opportunity: "💡",
  warning: "⚠️",
  positive: "✅",
  info: "ℹ️",
};

export const PATTERN_TONE: Record<string, Tone> = {
  opportunity: "indigo",
  warning: "declined",
  positive: "soft",
  info: "neutral",
};

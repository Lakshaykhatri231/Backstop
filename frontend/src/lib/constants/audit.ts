import type { Tone } from "./tone";

// Ported verbatim from static/index.html's AuditPage TYPE_COLOR (12 entries).
export const AUDIT_TYPE_TONE: Record<string, Tone> = {
  event_received: "loyal",
  dropoff_event_detected: "indigo",
  decision_made: "soft",
  action_executed: "soft",
  llm_failure_fallback: "declined",
  webhook_rejected_bad_signature: "failed",
  policy_recommendation_generated: "indigo",
  policy_recommendation_applied: "soft",
  failure_policy_recommendation_generated: "indigo",
  tier_policy_recommendation_generated: "loyal",
  tier_changed: "loyal",
  tier_reevaluation_committed: "loyal",
};

export function auditTypeTone(actionType: string): Tone {
  return AUDIT_TYPE_TONE[actionType] ?? "neutral";
}

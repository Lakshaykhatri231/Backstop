import { apiFetch } from "./client";

export type OutcomeSummary = {
  total_events: number;
  total_decisions: number;
  nudges_sent: number;
  escalated: number;
  escalated_by_confidence_gate: number;
  retries: number;
  no_action: number;
  failed: number;
  other: number;
};

export type EventDecision = {
  action: string;
  confidence: number;
  reasoning: string;
  source: string;
  escalated: boolean;
  outcome: string | null;
};

export type OutcomeEvent = {
  event_id: string;
  event_type: string;
  customer_name: string | null;
  customer_id: string | null;
  failure_reason: string | null;
  attempt_count: number;
  amount_inr: number;
  received_at: string;
  decision: EventDecision | null;
};

export type AuditLogEntry = {
  sequence_num: number;
  action_type: string;
  // Raw JSON text, NOT a parsed object — the backend writes audit details as
  // a JSON-encoded string column. Parse it client-side (see AuditLogTable's
  // parseDetails helper), with a raw-text fallback if it's ever not valid JSON.
  details: string;
  prev_hash: string;
  entry_hash: string;
  created_at: string;
};

export type AuditVerifyResult = {
  chain_intact: boolean;
  message: string;
};

// All open, no auth.
export function getOutcomes() {
  return apiFetch<OutcomeSummary>("/outcomes", { auth: false });
}

export function getEvents(limit = 50) {
  return apiFetch<OutcomeEvent[]>(`/outcomes/events?limit=${limit}`, { auth: false });
}

export function getAuditLog(limit = 100) {
  return apiFetch<AuditLogEntry[]>(`/audit/log?limit=${limit}`, { auth: false });
}

export function getAuditVerify() {
  return apiFetch<AuditVerifyResult>("/audit/verify", { auth: false });
}

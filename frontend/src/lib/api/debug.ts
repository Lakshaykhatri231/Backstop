import { apiFetch } from "./client";

// The client-side webhook-signing "Live Demo" panel is deliberately NOT
// resurrected (see project plan) — scripts/simulate_webhook.py remains the
// way to fire webhook scenarios. These two toggles are open, no-auth,
// low-risk demo levers and are fine to keep reachable from the UI.

export function toggleLlmFailure(forced: boolean) {
  return apiFetch<{ llm_failure_forced: boolean }>("/debug/toggle-llm-failure", {
    method: "POST",
    body: { forced },
    auth: false,
  });
}

export function simulateAbandonment(body?: {
  customer_id?: string;
  subscription_id?: string;
  amount_inr?: number;
  checkout_status?: "attempted" | "created";
  abandonment_count_override?: number;
}) {
  return apiFetch<Record<string, unknown>>("/debug/simulate-abandonment", {
    method: "POST",
    body: body ?? {},
    auth: false,
  });
}

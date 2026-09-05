import { apiFetch } from "./client";

export type NextTier = {
  tier: string;
  score_gap: number;
  attempts_gap: number;
};

export type CustomerStats = {
  total_orders: number;
  successful_orders: number;
  failed_orders: number;
  total_payment_attempts: number;
  engagement_score: number;
  score_components: Record<string, number>;
  tier: string;
  tier_reason: string;
  next_tier: NextTier | null;
  most_common_failure_reason: string | null;
};

export type Customer = {
  id: string;
  email: string;
  name: string;
  tier: string;
  stats: CustomerStats;
};

export type AuthResponse = {
  token: string;
  customer: Customer;
};

export function register(email: string, password: string, name: string) {
  return apiFetch<AuthResponse>("/auth/register", {
    method: "POST",
    body: { email, password, name },
    auth: false,
  });
}

export function login(email: string, password: string) {
  return apiFetch<AuthResponse>("/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export function me() {
  return apiFetch<Customer>("/auth/me");
}

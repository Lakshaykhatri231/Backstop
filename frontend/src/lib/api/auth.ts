import { apiFetch } from "./client";
import type { ContactStatus, Language } from "./contact";

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
  // Drives the "add your number" prompt; nothing is blocked on it.
  contact: ContactStatus;
};

export type AuthResponse = {
  token: string;
  customer: Customer;
};

export type Role = "customer" | "business" | "merchant";

export type AppAccount = {
  id: string;
  email: string;
  name: string;
  role: "merchant" | "business";
  business_account_id: string | null;
  company_name: string | null;
};

// A sign-in can be a shopper, the merchant or a business customer.
export type LoginResponse = {
  token: string;
  role: Role;
  customer?: Customer;
  account?: AppAccount;
};

export type WhoAmI = { role: Role; id: string; email: string; name: string; company_name?: string | null };

export type RegisterInput = {
  email: string;
  password: string;
  name: string;
  // Phone is required and must come with the proof from the OTP check.
  phone: string;
  phone_proof: string;
  preferred_language?: Language;
  voice_consent?: boolean;
};

export function register(input: RegisterInput) {
  return apiFetch<AuthResponse>("/auth/register", {
    method: "POST",
    body: input,
    auth: false,
  });
}

export function login(email: string, password: string) {
  return apiFetch<LoginResponse>("/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export function me() {
  return apiFetch<Customer>("/auth/me");
}

export function whoami() {
  return apiFetch<WhoAmI>("/auth/whoami");
}

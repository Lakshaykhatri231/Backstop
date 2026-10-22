import { apiFetch } from "./client";

// Real Razorpay Test Mode: card mandates we debit (/autopay/*) and the
// Payment Link registry (/payment-links/*). See app/autopay.py, app/payment_links.py.

export type LinkUsage = {
  mode: "real" | "stub";
  test_mode: boolean;
  cap: number;
  real_links_created_here: number;
  stand_ins: number;
  limit_hits: number;
  account_links_total?: number | null;
};

export type Mandate = {
  id: string;
  customer_name: string;
  status: "setup" | "active" | "rejected" | "cancelled" | "paused";
  amount_inr: number;
  max_amount_inr: number;
  frequency: string;
  razorpay_customer_id: string | null;
  token_id: string | null;
  registration_url: string | null;
  next_due_at: string | null;
  created_at: string;
};

export type DuplicatePayment = {
  payment_link_row: string;
  order_id: string;
  payment_id: string;
  amount_inr: number;
  paid_at: string | null;
};

export const getLinkUsage = () => apiFetch<LinkUsage>("/payment-links/usage");
export const listMandates = () => apiFetch<Mandate[]>("/autopay/mandates");
export const createAutopayScenario = (b: { name: string; phone: string; amount_inr: number }) =>
  apiFetch<{ mandate_id: string; registration_url: string | null; test_cards: Record<string, string> }>(
    "/demo/scenarios/autopay",
    { method: "POST", body: b },
  );
export const syncMandate = (id: string) =>
  apiFetch<{ status: string; registration_status?: string }>(`/autopay/mandates/${id}/sync`, { method: "POST" });
export const chargeNext = (id: string) =>
  apiFetch<{ pdn_at: string; debit_at: string }>(`/autopay/mandates/${id}/charge-next`, { method: "POST" });
export const cancelMandate = (id: string) => apiFetch<Mandate>(`/autopay/mandates/${id}/cancel`, { method: "POST" });
export const listDuplicatePayments = () => apiFetch<DuplicatePayment[]>("/payment-links/duplicates");
export const refundDuplicate = (row: string, paymentId: string) =>
  apiFetch<{ refund_id: string; status: string }>(`/payment-links/${row}/refund/${paymentId}`, { method: "POST" });

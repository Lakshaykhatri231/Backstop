import { apiFetch } from "./client";
import type { CustomerStats } from "./auth";

// Opaque pass-through — exact keys come from app/revenue.py::revenue_as_dict,
// which includes at minimum total revenue/recovered/lost and the three
// at-risk buckets (at_risk_soft, at_risk_declined, at_risk_failed).
export type RevenueState = Record<string, number> & {
  total_revenue: number;
  total_recovered: number;
  total_lost: number;
  total_at_risk: number;
  incentive_cost: number;
  at_risk_soft: number;
  at_risk_declined: number;
  at_risk_failed: number;
};

export type MerchantCustomer = {
  id: string;
  email: string;
  name: string;
  tier: string;
  created_at: string;
  stats: CustomerStats;
};

export type MerchantCartEvent = {
  id: string;
  customer_id: string;
  customer_name: string | null;
  event_type: string;
  amount_inr: number;
  tier_at_time: string;
  action: string | null;
  confidence: number | null;
  reasoning: string | null;
  outcome: string | null;
  status: string | null;
  incentive_pct: number | null;
  final_amount_inr: number | null;
  created_at: string;
  resolved_at: string | null;
};

export type MerchantOrder = {
  id: string;
  customer_name: string | null;
  razorpay_order_id: string;
  amount_inr: number;
  status: string;
  failure_reason: string | null;
  recovered_from_cart_event_id: string | null;
  created_at: string;
  resolved_at: string | null;
};

// All open, no auth — merchant dashboard has no login of its own.
export function getRevenue() {
  return apiFetch<RevenueState>("/merchant/revenue", { auth: false });
}

export function getCustomers(limit = 50) {
  return apiFetch<MerchantCustomer[]>(`/merchant/customers?limit=${limit}`, { auth: false });
}

export function getCartEvents(limit = 50) {
  return apiFetch<MerchantCartEvent[]>(`/merchant/cart-events?limit=${limit}`, { auth: false });
}

export function getOrders(limit = 50) {
  return apiFetch<MerchantOrder[]>(`/merchant/orders?limit=${limit}`, { auth: false });
}

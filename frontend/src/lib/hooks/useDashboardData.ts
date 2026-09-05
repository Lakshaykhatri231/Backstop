import { useQuery, useQueryClient } from "@tanstack/react-query";

import * as merchant from "../api/merchant";
import * as outcomes from "../api/outcomes";
import * as insights from "../api/insights";

// One shared TanStack Query hook per data need, each polling every 5s. This
// replaces the old dashboard's bug: two independent, duplicating
// setInterval loops (the root App poll + RevenuePage's own separate poll).
// Query-key dedup means mounting the same hook from two tabs at once still
// produces exactly one network request and one shared interval.
const POLL_MS = 5000;

export function useRevenue() {
  return useQuery({ queryKey: ["merchant", "revenue"], queryFn: merchant.getRevenue, refetchInterval: POLL_MS });
}

export function useCustomers(limit = 50) {
  return useQuery({
    queryKey: ["merchant", "customers", limit],
    queryFn: () => merchant.getCustomers(limit),
    refetchInterval: POLL_MS,
  });
}

export function useCartEvents(limit = 50) {
  return useQuery({
    queryKey: ["merchant", "cart-events", limit],
    queryFn: () => merchant.getCartEvents(limit),
    refetchInterval: POLL_MS,
  });
}

export function useOrders(limit = 50) {
  return useQuery({
    queryKey: ["merchant", "orders", limit],
    queryFn: () => merchant.getOrders(limit),
    refetchInterval: POLL_MS,
  });
}

export function useOutcomes() {
  return useQuery({ queryKey: ["outcomes", "summary"], queryFn: outcomes.getOutcomes, refetchInterval: POLL_MS });
}

export function useEvents(limit = 50) {
  return useQuery({
    queryKey: ["outcomes", "events", limit],
    queryFn: () => outcomes.getEvents(limit),
    refetchInterval: POLL_MS,
  });
}

// Fetch-once-on-mount parity with the original — no refetchInterval. Must be
// invalidated after apply-suggestion/tier-commit succeed (see the 3 analysis
// modals), which is why callers need the query key, not just this hook.
export const auditLogKey = (limit: number) => ["audit", "log", limit] as const;

export function useAuditLog(limit = 100) {
  return useQuery({ queryKey: auditLogKey(limit), queryFn: () => outcomes.getAuditLog(limit) });
}

export function useInvalidateAuditLog() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: ["audit", "log"] });
}

// Fetch-once, like useAuditLog — these are policy thresholds, not
// live-changing metrics, so no polling interval.
export function useTierConfig() {
  return useQuery({ queryKey: ["insights", "tier-config"], queryFn: insights.getTierConfig });
}

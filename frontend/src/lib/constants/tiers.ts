import type { Tone } from "./tone";

// Dashboard-scoped tier palette, ported verbatim from static/index.html's
// TIER_COLOR (distinct from the storefront's own simpler tier badge — see
// features/store/StoreHeader.tsx — which is fine, different surfaces).
export const TIER_TONE: Record<string, Tone> = {
  new: "loyal",
  casual: "neutral",
  regular: "indigo",
  loyal: "soft",
  risk: "failed",
};

export function tierTone(tier: string | null | undefined): Tone {
  if (!tier) return "neutral";
  return TIER_TONE[tier] ?? "neutral";
}

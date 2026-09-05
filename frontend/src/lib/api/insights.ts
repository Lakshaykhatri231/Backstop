import { apiFetch } from "./client";

export type AnalysisRange = "7d" | "30d" | "all";

export type Pattern = {
  kind: "opportunity" | "warning" | "positive" | "info";
  text: string;
};

export type Recommendation = {
  param: string;
  current_value: unknown;
  suggested_value: unknown;
  rationale: string;
  supporting_metric?: string;
  is_tier_threshold?: boolean;
};

// These LLM-analysis payloads are large (config_snapshot, buckets,
// breakdowns, etc.) — only the fields every modal actually renders are
// typed; the rest is intentionally left as unknown/permissive and read
// defensively by each modal.
export type AnalysisResult = {
  range: string;
  summary: string;
  patterns: Pattern[];
  recommendations: Recommendation[];
  llm_error: string | null;
  audit_sequence_num: number;
  [key: string]: unknown;
};

export function getIncentiveAnalysis(range: AnalysisRange = "30d") {
  return apiFetch<AnalysisResult>(`/insights/incentive-analysis?range=${range}`, { auth: false });
}

export function getRecoveryAnalysis(range: AnalysisRange = "30d") {
  return apiFetch<AnalysisResult>(`/insights/recovery-analysis?range=${range}`, { auth: false });
}

export function getTierAnalysis(range: AnalysisRange = "30d") {
  return apiFetch<AnalysisResult>(`/insights/tier-analysis?range=${range}`, { auth: false });
}

// Lightweight companion to tier-analysis, for the /tiers explainer page: no
// LLM call, no range-scoped aggregation, just current thresholds + weights.
export type TierConfig = {
  engagement_weights: {
    completion: number;
    frequency: number;
    monetary: number;
    recency: number;
    responsiveness: number;
  };
  tier_thresholds: {
    tier_loyal_score: number;
    tier_regular_score: number;
    tier_min_attempts_for_loyal: number;
    tier_min_attempts_for_regular: number;
    tier_target_orders_per_month: number;
    tier_target_aov_inr: number;
    tier_recency_window_days: number;
    tier_behavior_window_days: number;
    tier_risk_min_attempts: number;
    tier_risk_attributable_failure_rate: number;
    tier_risk_cancel_rate: number;
  };
  tier_distribution: Record<string, number>;
  incentive_config: {
    incentive_pct_bands: Record<string, [number, number]>;
    incentive_max_per_30d_by_tier: Record<string, number>;
    incentive_amount_caps_by_tier: Record<string, number>;
    incentive_eligible_tiers: string[];
    [key: string]: unknown;
  };
};

export function getTierConfig() {
  return apiFetch<TierConfig>("/insights/tier-config", { auth: false });
}

export type ApplySuggestionResult = {
  param: string;
  previous_value: unknown;
  new_value: unknown;
  applied: true;
  is_tier_threshold: boolean;
  note: string;
};

export function applySuggestion(args: {
  param: string;
  suggested_value: string;
  rationale?: string;
  supporting_metric?: string;
  analysis_sequence_num?: number;
}) {
  return apiFetch<ApplySuggestionResult>("/insights/apply-suggestion", {
    method: "POST",
    body: args,
    auth: false,
  });
}

// Builds the apply-suggestion payload from a Recommendation the way every
// analysis modal needs to — under exactOptionalPropertyTypes, an optional
// key can't be assigned `undefined` explicitly, so this conditionally
// includes each field instead of forcing every call site to repeat that.
export function applySuggestionForRecommendation(rec: Recommendation, analysisSequenceNum: number | undefined) {
  return applySuggestion({
    param: rec.param,
    suggested_value: String(rec.suggested_value),
    ...(rec.rationale ? { rationale: rec.rationale } : {}),
    ...(rec.supporting_metric ? { supporting_metric: rec.supporting_metric } : {}),
    ...(analysisSequenceNum != null ? { analysis_sequence_num: analysisSequenceNum } : {}),
  });
}

export type TierMove = {
  count: number;
  customers: { id: string; email: string; name: string }[];
};

export type TierReevaluationPreview = {
  total_customers: number;
  unchanged: number;
  moves: Record<string, TierMove>;
};

export function getTierReevaluationPreview(param?: string, value?: string) {
  const qs = param != null && value != null ? `?param=${encodeURIComponent(param)}&value=${encodeURIComponent(value)}` : "";
  return apiFetch<TierReevaluationPreview>(`/insights/tier-reevaluation-preview${qs}`, { auth: false });
}

export function commitTierReevaluation() {
  return apiFetch<{ total_customers: number; changed: number }>("/insights/tier-reevaluation-commit", {
    method: "POST",
    auth: false,
  });
}

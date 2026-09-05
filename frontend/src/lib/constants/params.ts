// Ported verbatim from static/index.html's PARAM_LABEL — machine param name
// -> human label, used across all 3 analysis modals' recommendation cards.
export const PARAM_LABEL: Record<string, string> = {
  incentive_max_order_value_casual: "Casual — max order value for discount (₹)",
  incentive_max_order_value_regular: "Regular — max order value for discount (₹)",
  incentive_max_order_value_loyal: "Loyal — max order value for discount (₹)",
  incentive_pct_casual_min: "Casual discount band — min (%)",
  incentive_pct_casual_max: "Casual discount band — max (%)",
  incentive_pct_regular_min: "Regular discount band — min (%)",
  incentive_pct_regular_max: "Regular discount band — max (%)",
  incentive_pct_loyal_min: "Loyal discount band — min (%)",
  incentive_pct_loyal_max: "Loyal discount band — max (%)",
  incentive_max_per_30d_casual: "Casual — offers per 30 days",
  incentive_max_per_30d_regular: "Regular — offers per 30 days",
  incentive_max_per_30d_loyal: "Loyal — offers per 30 days",
  nudge_expiry_hours: "Offer validity (hours)",
  casual_tier_incentive_eligible: "Casual tier incentive-eligible",
  confidence_threshold: "Confidence threshold",
  max_auto_retries: "Max auto retries",
  high_value_amount_inr: "High-value escalation floor (₹)",
  tier_loyal_score: "Engagement score for Loyal",
  tier_regular_score: "Engagement score for Regular",
  tier_min_attempts_for_loyal: "Min purchases for Loyal",
  tier_min_attempts_for_regular: "Min purchases for Regular",
  tier_target_orders_per_month: "Target purchases / month",
  tier_target_aov_inr: "Target avg order value (₹)",
  tier_recency_window_days: "Recency window (days)",
  tier_behavior_window_days: "Behaviour window (days)",
  tier_risk_min_attempts: "Min attempts before Risk applies",
  tier_risk_attributable_failure_rate: "Risk: customer-side failure rate",
  tier_risk_cancel_rate: "Risk: cancellation rate",
};

export function paramLabel(param: string): string {
  return PARAM_LABEL[param] ?? param;
}

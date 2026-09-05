import type { Tone } from "./tone";

// Ported verbatim from static/index.html's SOURCE_LABEL/SOURCE_COLOR.
export const SOURCE_LABEL: Record<string, string> = {
  llm_agent: "LLM",
  rules_engine: "Rules",
  rules_engine_fallback: "Fallback",
  customer_action: "Customer",
};

export const SOURCE_TONE: Record<string, Tone> = {
  llm_agent: "soft",
  rules_engine: "loyal",
  rules_engine_fallback: "declined",
  customer_action: "neutral",
};

export function sourceLabel(source: string | null | undefined): string {
  if (!source) return "Fallback";
  return SOURCE_LABEL[source] ?? "Fallback";
}

export function sourceTone(source: string | null | undefined): Tone {
  if (!source) return "declined";
  return SOURCE_TONE[source] ?? "declined";
}

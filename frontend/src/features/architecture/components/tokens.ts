export type FlowTone = "soft" | "declined" | "failed" | "indigo" | "loyal" | "brand" | "ink";

export const TONE_ICON_BG: Record<FlowTone, string> = {
  soft: "bg-soft/10 text-soft",
  declined: "bg-declined/10 text-declined",
  failed: "bg-failed/10 text-failed",
  indigo: "bg-indigo/10 text-indigo",
  loyal: "bg-loyal/10 text-loyal",
  brand: "bg-brand/10 text-brand",
  ink: "bg-ink/10 text-ink",
};

export const TONE_BORDER: Record<FlowTone, string> = {
  soft: "border-soft/20",
  declined: "border-declined/20",
  failed: "border-failed/20",
  indigo: "border-indigo/20",
  loyal: "border-loyal/20",
  brand: "border-brand/20",
  ink: "border-ink/10",
};

export const TONE_BORDER_STRONG: Record<FlowTone, string> = {
  soft: "border-soft/50",
  declined: "border-declined/60",
  failed: "border-failed/40",
  indigo: "border-indigo/50",
  loyal: "border-loyal/50",
  brand: "border-brand/50",
  ink: "border-ink/15",
};

export const TONE_DOT: Record<FlowTone, string> = {
  soft: "bg-soft",
  declined: "bg-declined",
  failed: "bg-failed",
  indigo: "bg-indigo",
  loyal: "bg-loyal",
  brand: "bg-brand",
  ink: "bg-ink",
};

export const TONE_TEXT: Record<FlowTone, string> = {
  soft: "text-soft",
  declined: "text-declined",
  failed: "text-failed",
  indigo: "text-indigo",
  loyal: "text-loyal",
  brand: "text-brand",
  ink: "text-ink/50",
};

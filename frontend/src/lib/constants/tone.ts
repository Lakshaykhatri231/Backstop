// Shared color vocabulary for every domain badge/label map in
// lib/constants/*.ts. Maps a small set of semantic tones onto this app's
// Tailwind design tokens (styles.css) instead of scattering raw hex/className
// strings across every table/badge component.
export type Tone = "soft" | "loyal" | "indigo" | "declined" | "failed" | "neutral";

const TONE_CLASSES: Record<Tone, { badge: string; text: string; dot: string }> = {
  soft: { badge: "bg-soft/15 text-soft", text: "text-soft", dot: "bg-soft" },
  loyal: { badge: "bg-loyal/15 text-loyal", text: "text-loyal", dot: "bg-loyal" },
  indigo: { badge: "bg-indigo/15 text-indigo", text: "text-indigo", dot: "bg-indigo" },
  declined: { badge: "bg-declined/15 text-declined", text: "text-declined", dot: "bg-declined" },
  failed: { badge: "bg-failed/15 text-failed", text: "text-failed", dot: "bg-failed" },
  neutral: { badge: "bg-ink/8 text-ink/50", text: "text-ink/50", dot: "bg-ink/30" },
};

export function toneClasses(tone: Tone | undefined): { badge: string; text: string; dot: string } {
  return TONE_CLASSES[tone ?? "neutral"];
}

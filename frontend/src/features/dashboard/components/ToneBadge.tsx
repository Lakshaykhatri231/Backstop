import { toneClasses, type Tone } from "@/lib/constants/tone";

export function ToneBadge({ label, tone }: { label: string; tone: Tone }) {
  const c = toneClasses(tone);
  return (
    <span className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] font-semibold whitespace-nowrap ${c.badge}`}>
      {label}
    </span>
  );
}

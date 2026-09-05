import { PATTERN_ICON, PATTERN_TONE } from "@/lib/constants/outcomes";
import { toneClasses } from "@/lib/constants/tone";
import type { Pattern } from "@/lib/api/insights";

export function PatternsList({ patterns, title = "What the data shows" }: { patterns: Pattern[]; title?: string }) {
  if (!patterns || patterns.length === 0) return null;

  return (
    <div className="mb-5">
      <h3 className="text-sm font-bold text-ink mb-2.5">{title}</h3>
      {patterns.map((p, i) => {
        const c = toneClasses(PATTERN_TONE[p.kind] ?? "neutral");
        return (
          <div key={i} className={`flex gap-2.5 items-start bg-cream rounded-md px-3 py-2.5 mb-2 border-l-[3px] ${c.dot.replace("bg-", "border-")}`}>
            <span className="text-[15px] leading-[19px]">{PATTERN_ICON[p.kind] ?? "•"}</span>
            <span className="text-[13px] text-ink leading-relaxed">{p.text}</span>
          </div>
        );
      })}
    </div>
  );
}

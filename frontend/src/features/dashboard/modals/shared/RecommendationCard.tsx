import { paramLabel } from "@/lib/constants/params";
import type { Recommendation } from "@/lib/api/insights";

export function RecommendationCard({
  rec,
  applied,
  applying,
  onImplement,
}: {
  rec: Recommendation;
  applied: boolean;
  applying: boolean;
  onImplement: () => void;
}) {
  return (
    <div className="bg-cream border border-ink/10 rounded-lg p-3 mb-2.5">
      <div className="flex justify-between items-center gap-3 flex-wrap">
        <div>
          <span className="inline-flex items-center rounded bg-indigo/15 text-indigo px-2 py-0.5 text-[11px] font-semibold">
            {paramLabel(rec.param)}
          </span>
          <span className="ml-2.5 text-sm">
            <span className="text-ink/40">{String(rec.current_value)}</span>
            <span className="text-ink/50"> → </span>
            <span className="text-soft font-bold">{String(rec.suggested_value)}</span>
          </span>
        </div>
        <button
          onClick={onImplement}
          disabled={applying || applied}
          className={`rounded-md px-3.5 py-1.5 text-xs font-bold whitespace-nowrap ${
            applied ? "bg-soft/15 text-soft" : "bg-ink text-cream disabled:opacity-60"
          }`}
        >
          {applied ? "✓ Applied" : applying ? "Applying…" : "Implement"}
        </button>
      </div>
      <p className="text-xs text-ink/50 mt-2">{rec.rationale}</p>
      {rec.supporting_metric && <p className="text-[11px] text-ink/40 mt-1">Based on: {rec.supporting_metric}</p>}
    </div>
  );
}

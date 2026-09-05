import type { AnalysisRange } from "@/lib/api/insights";

const RANGE_OPTIONS: { key: AnalysisRange; label: string }[] = [
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "all", label: "All time" },
];

export function RangeSelector({
  range,
  onChange,
  onAnalyze,
  loading,
  accentClass,
}: {
  range: AnalysisRange;
  onChange: (r: AnalysisRange) => void;
  onAnalyze: () => void;
  loading: boolean;
  accentClass: string;
}) {
  return (
    <div className="flex gap-2 mb-4 flex-wrap items-center">
      {RANGE_OPTIONS.map((opt) => (
        <button
          key={opt.key}
          onClick={() => onChange(opt.key)}
          className={`rounded-md px-3.5 py-1.5 text-xs font-semibold border ${
            range === opt.key ? "bg-ink text-cream border-ink" : "bg-transparent text-ink/60 border-ink/15"
          }`}
        >
          {opt.label}
        </button>
      ))}
      <button
        onClick={onAnalyze}
        disabled={loading}
        className={`ml-auto rounded-md px-4 py-1.5 text-xs font-bold disabled:opacity-50 ${accentClass}`}
      >
        {loading ? "Analyzing…" : "Analyze"}
      </button>
    </div>
  );
}

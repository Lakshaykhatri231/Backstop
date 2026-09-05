export function StatCard({
  label,
  value,
  tone = "default",
  sub,
}: {
  label: string;
  value: string | number;
  tone?: "default" | "soft" | "declined" | "failed" | "indigo" | "loyal";
  sub?: string;
}) {
  const color =
    tone === "soft"
      ? "text-soft"
      : tone === "declined"
        ? "text-declined"
        : tone === "failed"
          ? "text-failed"
          : tone === "indigo"
            ? "text-indigo"
            : tone === "loyal"
              ? "text-loyal"
              : "text-ink";

  return (
    <div className="rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] text-center">
      <p className={`font-display text-3xl font-semibold tabular-nums ${color}`}>{value}</p>
      <p className="text-xs text-ink/50 mt-1">{label}</p>
      {sub && <p className={`text-xs mt-1 ${color}`}>{sub}</p>}
    </div>
  );
}

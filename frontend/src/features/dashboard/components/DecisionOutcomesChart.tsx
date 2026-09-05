import { Bar, BarChart, CartesianGrid, Cell, XAxis } from "recharts";

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import type { OutcomeSummary } from "@/lib/api/outcomes";

// Tailwind can't resolve arbitrary CSS-var color names inside inline SVG fill
// attributes reliably across builds, so these mirror styles.css's tokens as
// literal hex — keep in sync with the @theme block if those ever change.
const COLORS = {
  indigo: "#6366f1",
  loyal: "#4f7cbb",
  declined: "#d9a441",
  neutral: "#9c9186",
  failed: "#c0492f",
  muted: "#c9beac",
};

const chartConfig = { value: { label: "Count" } } satisfies ChartConfig;

export function DecisionOutcomesChart({ outcomes }: { outcomes: OutcomeSummary }) {
  const data = [
    { name: "Nudged", value: outcomes.nudges_sent || 0, fill: COLORS.indigo },
    { name: "Retries", value: outcomes.retries || 0, fill: COLORS.loyal },
    { name: "Escalated", value: outcomes.escalated || 0, fill: COLORS.declined },
    { name: "No action", value: outcomes.no_action || 0, fill: COLORS.neutral },
    { name: "Failed", value: outcomes.failed || 0, fill: COLORS.failed },
    // Residual bucket — only rendered when something falls outside the named
    // ones, so a new outcome string can never silently vanish from the chart.
    ...(outcomes.other > 0 ? [{ name: "Other", value: outcomes.other, fill: COLORS.muted }] : []),
  ];

  return (
    <ChartContainer config={chartConfig} className="aspect-auto h-[180px] w-full">
      <BarChart data={data} margin={{ top: 8, left: 0, right: 0, bottom: 0 }}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="name" tickLine={false} axisLine={false} fontSize={12} />
        <ChartTooltip content={<ChartTooltipContent hideLabel />} />
        <Bar dataKey="value" radius={[4, 4, 0, 0]}>
          {data.map((d) => (
            <Cell key={d.name} fill={d.fill} />
          ))}
        </Bar>
      </BarChart>
    </ChartContainer>
  );
}

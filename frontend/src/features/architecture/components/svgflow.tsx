import { cn } from "@/lib/utils";

import type { FlowTone } from "./tokens";

/** Precise, coordinate-driven flowchart primitives — SVG shapes with computed connector paths
 * that actually meet shape boundaries, instead of CSS-approximated cards + arrow icons. Text is
 * measured (via wrapLabel) before a box's height is decided, so a box never clips its own label. */

const TONE_STROKE: Record<FlowTone, string> = {
  soft: "stroke-soft",
  declined: "stroke-declined",
  failed: "stroke-failed",
  indigo: "stroke-indigo",
  loyal: "stroke-loyal",
  brand: "stroke-brand",
  ink: "stroke-ink/25",
};

const TONE_FILL_SOFT: Record<FlowTone, string> = {
  soft: "fill-soft/[0.06]",
  declined: "fill-declined/[0.06]",
  failed: "fill-failed/[0.06]",
  indigo: "fill-indigo/[0.06]",
  loyal: "fill-loyal/[0.06]",
  brand: "fill-brand/[0.06]",
  ink: "fill-white",
};

export function wrapLabel(text: string, maxChars: number): string[] {
  const words = text.split(" ");
  const lines: string[] = [];
  let current = "";
  for (const w of words) {
    const candidate = current ? `${current} ${w}` : w;
    if (candidate.length > maxChars && current) {
      lines.push(current);
      current = w;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  return lines;
}

export function SvgLabel({
  x,
  y,
  lines,
  className,
  lineHeight = 13,
  anchor = "middle",
}: {
  x: number;
  y: number;
  lines: string[];
  className?: string;
  lineHeight?: number;
  anchor?: "start" | "middle" | "end";
}) {
  const startDy = -((lines.length - 1) * lineHeight) / 2;
  return (
    <text x={x} y={y} textAnchor={anchor} className={className}>
      {lines.map((line, i) => (
        <tspan key={i} x={x} dy={i === 0 ? startDy : lineHeight}>
          {line}
        </tspan>
      ))}
    </text>
  );
}

// Wrap widths (chars/line) and line heights are calibrated together to the font sizes actually
// rendered below — LABEL_WRAP/SUB_WRAP shrink when a font grows so a box's fixed 300-unit width
// still holds however many characters now fit, and processBoxHeight uses the same line heights
// ProcessBox renders with, so a box is always sized to the text it's about to draw.
const LABEL_WRAP = 26;
const SUB_WRAP = 30;
const LABEL_LH = 16;
const SUB_LH = 13;
const BOX_PAD = 18;
const LABEL_SUB_GAP = 7;

/** Returns the box height a ProcessBox needs for its (already-wrapped) label + optional sub. */
export function processBoxHeight(labelLines: number, subLines: number): number {
  const labelH = labelLines * LABEL_LH;
  const subH = subLines > 0 ? subLines * SUB_LH + LABEL_SUB_GAP : 0;
  return Math.max(52, BOX_PAD + labelH + subH);
}

/** Single source of truth for a ProcessBox's height — call once in layout, reuse for render. */
export function measureProcessBox(label: string, sub?: string): number {
  const labelLines = wrapLabel(label, LABEL_WRAP).length;
  const subLines = sub ? wrapLabel(sub, SUB_WRAP).length : 0;
  return processBoxHeight(labelLines, subLines);
}

export function ProcessBox({
  x,
  y,
  w,
  h,
  label,
  sub,
  tone = "ink",
  dashed = false,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  sub?: string;
  tone?: FlowTone;
  dashed?: boolean;
}) {
  const labelLines = wrapLabel(label, LABEL_WRAP);
  const subLines = sub ? wrapLabel(sub, SUB_WRAP) : [];
  const cx = x + w / 2;

  // Stack the label block then the sub block by their actual measured heights (not a fixed
  // offset) so a 2-line label next to a 4-line sub never overlaps — only their total height
  // changes, never their relative position.
  const gap = subLines.length > 0 ? LABEL_SUB_GAP : 0;
  const labelBlockH = labelLines.length * LABEL_LH;
  const subBlockH = subLines.length * SUB_LH;
  const contentTop = y + (h - (labelBlockH + gap + subBlockH)) / 2;
  const labelY = contentTop + labelBlockH / 2 + 5;
  const subY = contentTop + labelBlockH + gap + subBlockH / 2 + 4;

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        rx={10}
        className={cn("fill-white", TONE_STROKE[tone])}
        strokeWidth={dashed ? 1.5 : 1.75}
        strokeDasharray={dashed ? "5 4" : undefined}
      />
      <SvgLabel
        x={cx}
        y={labelY}
        lines={labelLines}
        className="fill-ink text-[14px] font-semibold"
        lineHeight={LABEL_LH}
      />
      {subLines.length > 0 && (
        <SvgLabel
          x={cx}
          y={subY}
          lines={subLines}
          className="fill-ink/55 text-[12px]"
          lineHeight={SUB_LH}
        />
      )}
    </g>
  );
}

export function DecisionDiamond({
  cx,
  cy,
  halfW = 108,
  halfH = 58,
  label,
  tone,
}: {
  cx: number;
  cy: number;
  halfW?: number;
  halfH?: number;
  label: string;
  tone: FlowTone;
}) {
  const points = [
    `${cx},${cy - halfH}`,
    `${cx + halfW},${cy}`,
    `${cx},${cy + halfH}`,
    `${cx - halfW},${cy}`,
  ].join(" ");
  // Wrap width scales with halfW (wider diamond, longer line) and is capped at 2 lines — a
  // diamond narrows fast away from its vertical center, so a 3rd line pokes past the points.
  const maxLineChars = Math.max(8, Math.floor(halfW / 9));
  const lines = wrapLabel(label, maxLineChars).slice(0, 2);
  return (
    <g>
      <polygon
        points={points}
        className={cn(TONE_FILL_SOFT[tone], TONE_STROKE[tone])}
        strokeWidth={2}
      />
      <SvgLabel
        x={cx}
        y={cy + 5}
        lines={lines}
        className="fill-ink text-[13px] font-semibold"
        lineHeight={15}
      />
    </g>
  );
}

type TerminalTone = "start" | "recovered" | "lost" | "escalated";

const TERMINAL_STYLE: Record<TerminalTone, string> = {
  start: "fill-ink stroke-ink",
  recovered: "fill-soft stroke-soft",
  lost: "fill-white stroke-ink/35",
  escalated: "fill-declined stroke-declined",
};

const TERMINAL_TEXT: Record<TerminalTone, string> = {
  start: "fill-cream",
  recovered: "fill-white",
  lost: "fill-ink/55",
  escalated: "fill-ink",
};

export function Terminal({
  cx,
  cy,
  label,
  tone,
  w = 128,
  h = 34,
}: {
  cx: number;
  cy: number;
  label: string;
  tone: TerminalTone;
  w?: number;
  h?: number;
}) {
  return (
    <g>
      <rect
        x={cx - w / 2}
        y={cy - h / 2}
        width={w}
        height={h}
        rx={h / 2}
        className={TERMINAL_STYLE[tone]}
        strokeWidth={1.75}
        strokeDasharray={tone === "lost" ? "4 3" : undefined}
      />
      <text
        x={cx}
        y={cy + 5}
        textAnchor="middle"
        className={cn("text-[13px] font-bold", TERMINAL_TEXT[tone])}
      >
        {label}
      </text>
    </g>
  );
}

/** A straight or elbowed connector with an arrowhead, plus an optional label at the bend/midpoint.
 * The label sits on a solid backing rect (the standard diagram-tool "knockout") so it stays
 * legible regardless of whether a line segment happens to pass under it. Keep labels short —
 * a word or three — long sentences belong in the box text or the caption below the figure. */
export function Connector({
  points,
  markerId,
  label,
  labelAt,
  className,
  labelClassName,
  dashed = false,
  maxChars = 16,
}: {
  points: { x: number; y: number }[];
  markerId: string;
  label?: string;
  labelAt?: { x: number; y: number; anchor?: "start" | "middle" | "end" };
  className?: string;
  /** Explicit fill class for the label text. `className?.replace("stroke-", "fill-")` is used as
   * a fallback when omitted, but that's a runtime string — Tailwind's JIT scanner can only see
   * class names that appear literally in source, so a derived "fill-x/NN" silently renders with
   * no color unless that exact literal happens to already exist elsewhere in the codebase. Pass
   * labelClassName explicitly (a real string literal at the call site) whenever className carries
   * an opacity modifier, to actually get that color. */
  labelClassName?: string;
  dashed?: boolean;
  maxChars?: number;
}) {
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
  const lines = label ? wrapLabel(label, maxChars) : [];
  const lineHeight = 13;
  const charW = 6.4;
  const textW = lines.length ? Math.max(...lines.map((l) => l.length)) * charW + 6 : 0;
  const textH = lines.length * lineHeight + 3;
  const anchor = labelAt?.anchor ?? "middle";
  const lx = labelAt?.x ?? 0;
  const bgX = anchor === "end" ? lx - textW : anchor === "start" ? lx : lx - textW / 2;
  return (
    <g>
      <path
        d={d}
        fill="none"
        strokeWidth={1.75}
        strokeDasharray={dashed ? "4 3" : undefined}
        className={className ?? "stroke-ink/30"}
        markerEnd={`url(#${markerId})`}
      />
      {label && labelAt && (
        <g>
          <rect
            x={bgX - 3}
            y={labelAt.y - textH / 2 - 2}
            width={textW + 6}
            height={textH + 4}
            className="fill-cream"
          />
          <SvgLabel
            x={labelAt.x}
            y={labelAt.y}
            lines={lines}
            anchor={anchor}
            lineHeight={lineHeight}
            className={cn(
              "text-[12px] font-semibold",
              labelClassName ?? className?.replace("stroke-", "fill-"),
            )}
          />
        </g>
      )}
    </g>
  );
}

export function ArrowMarker({ id, className }: { id: string; className?: string }) {
  return (
    <marker
      id={id}
      viewBox="0 0 10 10"
      refX="8.5"
      refY="5"
      markerWidth="7"
      markerHeight="7"
      orient="auto-start-reverse"
    >
      <path d="M0,0 L10,5 L0,10 z" className={className ?? "fill-ink/30"} />
    </marker>
  );
}

import type { LucideIcon } from "lucide-react";
import { ArrowDown, RotateCcw } from "lucide-react";

import { cn } from "@/lib/utils";

import { TONE_BORDER, TONE_BORDER_STRONG, TONE_DOT, TONE_TEXT, type FlowTone } from "./tokens";

/** Classic flowchart shapes (terminal / process / decision) for a narrative, single-path-at-a-time
 * story diagram — deliberately distinct from FlowNode's card grid so the two diagrams don't blur
 * into one visual language. */

export function Down({ className }: { className?: string }) {
  return <ArrowDown className={cn("size-4 text-ink/25 mx-auto shrink-0", className)} />;
}

type TerminalTone = "start" | "recovered" | "lost" | "escalated";

const TERMINAL_STYLE: Record<TerminalTone, string> = {
  start: "bg-ink border-ink text-cream",
  recovered: "bg-soft border-soft text-white",
  lost: "bg-transparent border-ink/25 border-dashed text-ink/45",
  escalated: "bg-declined border-declined text-ink",
};

export function Terminal({
  tone,
  label,
  icon: Icon,
  className,
}: {
  tone: TerminalTone;
  label: string;
  icon?: LucideIcon;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border-2 px-4 py-2 text-[11px] font-semibold justify-center whitespace-nowrap",
        TERMINAL_STYLE[tone],
        className,
      )}
    >
      {Icon && <Icon className="size-3.5 shrink-0" />}
      {label}
    </div>
  );
}

export function Step({
  tone,
  title,
  description,
  footer,
  className,
}: {
  tone: FlowTone;
  title: string;
  description?: string;
  footer?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-xl bg-white border p-3.5 text-center shadow-[0_16px_32px_-26px_rgba(43,29,18,0.35)]",
        TONE_BORDER[tone],
        className,
      )}
    >
      <p className="font-display font-semibold text-[12.5px] leading-snug text-ink">{title}</p>
      {description && <p className="text-[11px] text-ink/55 mt-1.5 leading-snug">{description}</p>}
      {footer}
    </div>
  );
}

export function LoopBadge({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-1.5 mt-2.5 pt-2 border-t border-dashed border-failed/25 text-[10px] text-failed/85 text-left leading-snug">
      <RotateCcw className="size-3 shrink-0 mt-0.5" />
      <span>{children}</span>
    </div>
  );
}

export function Decision({
  tone,
  label,
  size = "md",
}: {
  tone: FlowTone;
  label: string;
  size?: "md" | "sm";
}) {
  const dim = size === "md" ? "size-[130px] sm:size-[150px]" : "size-[108px] sm:size-[122px]";
  return (
    <div className="flex justify-center py-1">
      <div
        className={cn(
          "relative shrink-0 rotate-45 rounded-2xl border-2 bg-white",
          dim,
          TONE_BORDER_STRONG[tone],
        )}
      >
        <div className="absolute inset-0 -rotate-45 flex items-center justify-center p-3">
          <p className="text-[11px] font-semibold text-ink text-center leading-tight">{label}</p>
        </div>
      </div>
    </div>
  );
}

export function BranchLabel({ tone, text }: { tone: FlowTone; text: string }) {
  return (
    <div className="flex flex-col items-center gap-1">
      <span
        className={cn(
          "text-[11px] font-semibold text-center leading-tight max-w-[10rem]",
          TONE_TEXT[tone],
        )}
      >
        {text}
      </span>
      <ArrowDown className={cn("size-3.5", TONE_TEXT[tone])} />
    </div>
  );
}

export function ColumnHeader({
  tone,
  index,
  label,
}: {
  tone: FlowTone;
  index: number;
  label: string;
}) {
  return (
    <div className="flex items-center justify-center gap-2 mb-0.5">
      <span
        className={cn(
          "size-5 rounded-full grid place-items-center text-[10px] font-bold text-white shrink-0",
          TONE_DOT[tone],
        )}
      >
        {index}
      </span>
      <span className="font-display font-semibold text-sm text-ink">{label}</span>
    </div>
  );
}

export function TwoExit({
  left,
  right,
}: {
  left: { label: string; terminal: React.ReactNode };
  right: { label: string; terminal: React.ReactNode };
}) {
  return (
    <div className="grid grid-cols-2 gap-2 pt-0.5">
      {[left, right].map((exit) => (
        <div key={exit.label} className="flex flex-col items-center gap-1">
          <span className="text-[10px] font-medium text-ink/45 text-center leading-tight">
            {exit.label}
          </span>
          <ArrowDown className="size-3 text-ink/25" />
          {exit.terminal}
        </div>
      ))}
    </div>
  );
}

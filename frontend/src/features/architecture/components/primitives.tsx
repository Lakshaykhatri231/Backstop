import type { LucideIcon } from "lucide-react";
import { ArrowDown, ArrowRight } from "lucide-react";

import { cn } from "@/lib/utils";

import { TONE_BORDER, TONE_DOT, TONE_ICON_BG, type FlowTone } from "./tokens";

export type { FlowTone };

export function ToneDot({ tone, className }: { tone: FlowTone; className?: string }) {
  return <span className={cn("inline-block rounded-full size-2", TONE_DOT[tone], className)} />;
}

/** Node in a flow/pipeline diagram. `skipped` renders a faded dashed placeholder — used to show a
 * stage a given path deliberately does not go through, so the gap itself stays informative. */
export function FlowNode({
  icon: Icon,
  tone = "ink",
  title,
  description,
  meta,
  badge,
  emphasis = false,
  skipped = false,
  footer,
  className,
}: {
  icon?: LucideIcon;
  tone?: FlowTone;
  title: string;
  description?: string;
  meta?: string;
  badge?: string;
  emphasis?: boolean;
  skipped?: boolean;
  footer?: React.ReactNode;
  className?: string;
}) {
  if (skipped) {
    return (
      <div
        className={cn(
          "rounded-2xl border border-dashed border-ink/15 p-4 flex flex-col items-center justify-center text-center gap-1 min-h-[92px]",
          className,
        )}
      >
        <p className="text-xs font-medium text-ink/35">{title}</p>
        {description && <p className="text-[11px] text-ink/30 leading-snug">{description}</p>}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "rounded-2xl bg-white p-4 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.35)] border",
        TONE_BORDER[tone],
        emphasis && "bg-ink border-ink text-cream shadow-[0_24px_48px_-20px_rgba(43,29,18,0.55)]",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {Icon && (
            <span
              className={cn(
                "size-7 rounded-lg grid place-items-center shrink-0",
                emphasis ? "bg-cream/15 text-cream" : TONE_ICON_BG[tone],
              )}
            >
              <Icon className="size-4" />
            </span>
          )}
          <p
            className={cn(
              "font-display font-semibold text-sm leading-tight",
              emphasis && "text-cream",
            )}
          >
            {title}
          </p>
        </div>
        {badge && (
          <span
            className={cn(
              "text-[9.5px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-full shrink-0 whitespace-nowrap",
              emphasis ? "bg-cream/15 text-cream/80" : "bg-ink/5 text-ink/45",
            )}
          >
            {badge}
          </span>
        )}
      </div>
      {description && (
        <p
          className={cn(
            "text-[12px] leading-snug mt-2",
            emphasis ? "text-cream/75" : "text-ink/60",
          )}
        >
          {description}
        </p>
      )}
      {meta && (
        <p
          className={cn(
            "text-[10px] font-mono mt-2.5 pt-2 border-t",
            emphasis ? "border-cream/15 text-cream/50" : "border-ink/5 text-ink/35",
          )}
        >
          {meta}
        </p>
      )}
      {footer}
    </div>
  );
}

/** Vertical connector used between stacked rows of a lane diagram — always points down, since
 * lanes reflow from a horizontal grid to a stack but rows themselves never go side-by-side. */
export function FlowConnector({ label, muted = false }: { label?: string; muted?: boolean }) {
  return (
    <div className="flex items-center justify-center gap-2 py-0.5">
      {label && (
        <span
          className={cn(
            "text-[10px] leading-tight font-medium text-center px-2 py-0.5 rounded-full border whitespace-nowrap",
            muted ? "text-ink/30 border-ink/10" : "text-ink/50 border-ink/10 bg-ink/[0.03]",
          )}
        >
          {label}
        </span>
      )}
      <ArrowDown className={cn("size-3.5 shrink-0", muted ? "text-ink/20" : "text-ink/30")} />
    </div>
  );
}

/** Inline sequence arrow — down on mobile (stacked), right on desktop (row). */
export function InlineArrow({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center justify-center text-ink/25 shrink-0", className)}>
      <ArrowDown className="size-4 lg:hidden" />
      <ArrowRight className="hidden lg:block size-4" />
    </div>
  );
}

/** 1/2/3-column responsive grid — used both for a row of lane nodes and a row of connectors
 * beneath it, so columns always line up. */
export function LaneGrid({
  children,
  columns = 3,
}: {
  children: React.ReactNode;
  columns?: 2 | 3;
}) {
  return (
    <div
      className={cn("grid grid-cols-1 gap-4", columns === 3 ? "lg:grid-cols-3" : "sm:grid-cols-2")}
    >
      {children}
    </div>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description?: string;
}) {
  return (
    <div className="max-w-2xl mb-8">
      <p className="text-[11px] uppercase tracking-[0.18em] text-ember font-semibold mb-2">
        {eyebrow}
      </p>
      <h2 className="font-display text-2xl sm:text-3xl font-semibold tracking-tight">{title}</h2>
      {description && <p className="text-ink/60 text-sm leading-relaxed mt-3">{description}</p>}
    </div>
  );
}

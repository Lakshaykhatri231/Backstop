import type { ComponentType } from "react";

// Small hand-drawn glyphs per SKU, not fetched product photography — keeps the
// storefront fully self-contained (no external image hosting/licensing to
// manage for a demo app) while still giving each catalog card a distinct,
// on-brand visual instead of a bare text row.
const ICONS: Record<string, ComponentType<{ className?: string }>> = {
  sku_001: EarbudsIcon,
  sku_002: KeyboardIcon,
  sku_003: WatchIcon,
  sku_004: YogaMatIcon,
  sku_005: EspressoMachineIcon,
  sku_006: SpeakerIcon,
};

const TILE_BG = [
  "from-amber-100 to-orange-100",
  "from-orange-100 to-rose-100",
  "from-amber-100 to-yellow-100",
  "from-emerald-50 to-amber-100",
  "from-orange-100 to-amber-200",
  "from-rose-100 to-amber-100",
];

function tileIndex(sku: string): number {
  const n = Number(sku.replace(/\D/g, "")) || 0;
  return n % TILE_BG.length;
}

// No default sizing/rounding baked in — callers pass those via className
// (e.g. "aspect-square w-full rounded-xl" for a catalog tile, "size-9
// rounded-lg" for a cart-line thumbnail) so there's never a Tailwind
// class-order conflict between a component default and a caller override.
export function ProductIcon({ sku, className = "" }: { sku: string; className?: string }) {
  const Icon = ICONS[sku] ?? GenericBoxIcon;
  return (
    <div className={`bg-gradient-to-br ${TILE_BG[tileIndex(sku)]} grid place-items-center ${className}`}>
      <Icon className="w-[52%] h-[52%] text-ink/70" />
    </div>
  );
}

function EarbudsIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="20" cy="24" r="9" />
      <path d="M20 33 L17 48 Q16 52 20 52 Q24 52 23 48 L22 40" />
      <circle cx="44" cy="24" r="9" />
      <path d="M44 33 L41 48 Q40 52 44 52 Q48 52 47 48 L46 40" />
    </svg>
  );
}

function KeyboardIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="8" y="20" width="48" height="28" rx="4" />
      {[0, 1, 2, 3].map((col) =>
        [0, 1].map((row) => (
          <rect key={`${col}-${row}`} x={14 + col * 10} y={26 + row * 9} width="6" height="6" rx="1.2" />
        )),
      )}
    </svg>
  );
}

function WatchIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M24 12 H40 V20 H24 Z" />
      <path d="M24 44 H40 V52 H24 Z" />
      <rect x="18" y="20" width="28" height="24" rx="7" />
      <path d="M46 27 H50 V31 H46" />
    </svg>
  );
}

function YogaMatIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="7" y="25" width="50" height="14" rx="7" />
      <path d="M19 20 V44" strokeWidth="2" />
      <path d="M45 20 V44" strokeWidth="2" />
    </svg>
  );
}

function EspressoMachineIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="12" y="8" width="36" height="18" rx="3" />
      <line x1="12" y1="18" x2="48" y2="18" strokeWidth="1.8" opacity="0.5" />
      <circle cx="18" cy="13" r="1.4" fill="currentColor" stroke="none" />
      <path d="M27 26 L24 34 H38 L35 26 Z" />
      <rect x="22" y="42" width="18" height="11" rx="2" />
      <path d="M46 10 Q54 10 54 17 Q54 22 49 22" strokeWidth="2" />
    </svg>
  );
}

function SpeakerIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="12" y="10" width="40" height="44" rx="8" />
      <circle cx="32" cy="26" r="7" />
      <circle cx="32" cy="44" r="4" />
    </svg>
  );
}

function GenericBoxIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" fill="none" className={className} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22 L32 12 L52 22 L32 32 Z" />
      <path d="M12 22 V44 L32 54 V32" />
      <path d="M52 22 V44 L32 54" />
    </svg>
  );
}

// The mockup's "Insight layer" card was hardcoded fake LLM copy with no
// backing endpoint. Rather than fabricate new fake numbers, or trigger an
// LLM-backed analysis call just for a teaser (duplicating cost with the
// Audit Log tab's own 3 analysis modals), this points to where the real
// thing lives.
export function InsightLayerCard({ onOpenAuditLog }: { onOpenAuditLog: () => void }) {
  return (
    <div className="rounded-2xl bg-gradient-to-br from-ink to-[#4a2c12] text-cream p-6 shadow-[0_28px_56px_-24px_rgba(43,29,18,0.6)]">
      <div className="flex items-center gap-2 mb-3">
        <span className="size-6 rounded-md bg-indigo/20 grid place-items-center text-indigo text-xs font-bold">AI</span>
        <h2 className="font-display font-semibold text-lg">Insight layer</h2>
      </div>
      <p className="text-cream/85 text-sm leading-relaxed max-w-xl">
        The rules engine stays deterministic and bounded. The model only refines within those bounds — then explains
        policy performance in plain language and proposes configuration changes you can apply in one click, from the
        Audit Log tab.
      </p>
      <div className="flex flex-wrap items-center gap-2 mt-5">
        <button
          onClick={onOpenAuditLog}
          className="px-4 py-2 rounded-lg bg-tangerine text-ink text-sm font-semibold shadow-[0_10px_24px_-8px_rgba(245,158,11,0.8)]"
        >
          Open policy insights
        </button>
        <span className="text-xs text-cream/50">Append-only audit log · deterministic bounds enforced</span>
      </div>
    </div>
  );
}

export function AgentNoticeBanner({ notice, onDismiss }: { notice: string | null; onDismiss: () => void }) {
  if (!notice) return null;

  return (
    <div className="max-w-[640px] mx-auto mt-4 p-3 rounded-lg bg-declined/10 border border-declined/30 flex gap-3 items-start">
      <span className="flex-1 text-xs text-ink/60">{notice}</span>
      <button onClick={onDismiss} className="text-ink/40 hover:text-ink text-base leading-none">
        ×
      </button>
    </div>
  );
}

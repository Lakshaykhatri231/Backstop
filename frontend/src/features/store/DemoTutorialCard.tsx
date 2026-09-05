export function DemoTutorialCard({
  cartEmpty,
  busy,
  hasFailureNotice,
  onSimulateTimeout,
  onDeleteCart,
  onGiveUp,
}: {
  cartEmpty: boolean;
  busy: boolean;
  hasFailureNotice: boolean;
  onSimulateTimeout: () => void;
  onDeleteCart: () => void;
  onGiveUp: () => void;
}) {
  return (
    <div className="rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)] mb-6">
      <div className="flex flex-wrap items-end justify-between gap-2 mb-3">
        <div>
          <p className="font-display font-semibold text-sm">Try the recovery agent</p>
          <p className="text-xs text-ink/40 mt-0.5">
            Two distinct drop-off scenarios, handled differently by the agent based on your tier and history.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          onClick={onSimulateTimeout}
          disabled={busy || cartEmpty}
          className="flex-1 min-w-[220px] text-left rounded-lg bg-loyal/15 text-loyal text-xs font-semibold px-3 py-2.5 transition-colors duration-150 hover:bg-loyal/40 disabled:opacity-40 disabled:hover:bg-loyal/15"
        >
          ⏱ Simulate cart timeout (silent abandon)
        </button>
        <button
          onClick={onDeleteCart}
          disabled={busy || cartEmpty}
          className="flex-1 min-w-[220px] text-left rounded-lg bg-declined/15 text-declined text-xs font-semibold px-3 py-2.5 transition-colors duration-150 hover:bg-declined/40 disabled:opacity-40 disabled:hover:bg-declined/15"
        >
          🗑 Delete cart (explicit cancel)
        </button>
        {hasFailureNotice && (
          <button
            onClick={onGiveUp}
            disabled={busy}
            className="flex-1 min-w-[220px] text-left rounded-lg bg-failed/15 text-failed text-xs font-semibold px-3 py-2.5 transition-colors duration-150 hover:bg-failed/40 disabled:opacity-40 disabled:hover:bg-failed/15"
          >
            🏳 Give up on failed payment
          </button>
        )}
      </div>
    </div>
  );
}

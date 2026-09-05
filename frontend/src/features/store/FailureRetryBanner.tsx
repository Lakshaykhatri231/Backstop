import { Link } from "@tanstack/react-router";

import type { FailureNoticeState } from "@/lib/hooks/useStorefront";

export function FailureRetryBanner({
  notice,
  busy,
  onRetry,
  onDismiss,
}: {
  notice: FailureNoticeState | null;
  busy: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  if (!notice) return null;
  const isEscalated = notice.action === "escalate_to_human";

  return (
    <div
      className={`max-w-[640px] mx-auto mt-5 p-4 rounded-xl border ${
        isEscalated ? "bg-declined/10 border-declined/40" : "bg-soft/10 border-soft/40"
      }`}
    >
      <div className="flex justify-end -mt-1">
        <button onClick={onDismiss} className="text-ink/40 hover:text-ink text-base leading-none">
          ×
        </button>
      </div>
      <div className="text-center -mt-2">
        <p className="text-sm text-ink mb-3">{notice.message}</p>
        {notice.action === "retry_now" && (
          <button
            onClick={onRetry}
            disabled={busy}
            className="px-6 py-2.5 rounded-lg bg-soft text-white text-sm font-semibold mr-2 disabled:opacity-50"
          >
            Retry payment
          </button>
        )}
        {isEscalated && (
          <Link
            to="/support"
            className="inline-block px-5 py-2 rounded-lg border border-declined/50 text-declined text-xs font-semibold"
          >
            Contact support
          </Link>
        )}
      </div>
    </div>
  );
}

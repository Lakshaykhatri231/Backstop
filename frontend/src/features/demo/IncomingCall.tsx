import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { replyToCall, type CallResult, type RingingCall } from "@/lib/api/demo";

import { prefetchOpening } from "./callAudio";
import { audioReady, startRing, stopRing, unlockAudio } from "./ringtone";

const RING_SECONDS = 30;

// What a phone shows when a call arrives. Accepting opens the call itself
// (CallSimulator); declining or letting it ring out records "no answer", the
// same outcome a real unanswered call has, so the system's call-back logic runs.
export function IncomingCall({
  call, onAccept, onResolved,
}: {
  call: RingingCall;
  onAccept: () => void;
  onResolved: (r: CallResult | null, how: "declined" | "missed") => void;
}) {
  const [left, setLeft] = useState(RING_SECONDS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sound, setSound] = useState(audioReady());

  useEffect(() => {
    prefetchOpening(call.call_id);
    startRing();
    const hint = window.setInterval(() => setSound(audioReady()), 500);
    return () => {
      stopRing();
      window.clearInterval(hint);
    };
  }, []);

  useEffect(() => {
    if (busy) return;
    if (left <= 0) {
      void end("missed");
      return;
    }
    const t = window.setTimeout(() => setLeft((n) => n - 1), 1000);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [left, busy]);

  async function end(how: "declined" | "missed") {
    if (busy) return;
    setBusy(true);
    stopRing();
    try {
      const r = await replyToCall(call.call_id, "");
      onResolved(r, how);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not end the call");
      setBusy(false);
    }
  }

  function accept() {
    if (busy) return;
    unlockAudio();   // the click is the gesture that lets the voice play
    stopRing();
    onAccept();
  }

  const lang = call.language === "hi" ? "Hindi" : call.language === "en" ? "English" : "Hinglish";

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-gradient-to-b from-slate-900/95 to-slate-950/95 text-white">
      <div className="w-full max-w-sm text-center">
        <p className="text-xs tracking-[0.25em] uppercase text-white/50">Incoming call</p>

        <div className="relative mx-auto mt-8 size-28">
          <span className="absolute inset-0 rounded-full bg-emerald-400/20 animate-ping" />
          <span className="absolute -inset-3 rounded-full bg-emerald-400/10 animate-pulse" />
          <div className="relative size-28 rounded-full bg-gradient-to-br from-slate-600 to-slate-800 border border-white/10 grid place-items-center text-4xl font-semibold">
            {call.caller.trim().charAt(0).toUpperCase()}
          </div>
        </div>

        <h2 className="mt-6 text-2xl font-semibold leading-tight">{call.caller}</h2>
        <p className="mt-1 text-sm text-white/60">
          Calling {call.phone_masked} · {lang} · ringing {left}s
        </p>

        {!sound && (
          <p className="mt-4 text-xs text-amber-300/90">🔇 Click anywhere once to turn the ringtone on.</p>
        )}
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}

        <div className="mt-14 flex items-start justify-center gap-16">
          <div className="flex flex-col items-center gap-2">
            <button
              type="button"
              onClick={() => void end("declined")}
              disabled={busy}
              aria-label="Decline call"
              className="size-16 rounded-full bg-red-500 hover:bg-red-400 disabled:opacity-50 grid place-items-center text-2xl shadow-lg shadow-red-900/40"
            >
              <span className="rotate-[135deg] inline-block">📞</span>
            </button>
            <span className="text-xs text-white/60">Decline</span>
          </div>
          <div className="flex flex-col items-center gap-2">
            <button
              type="button"
              onClick={accept}
              disabled={busy}
              aria-label="Accept call"
              className="size-16 rounded-full bg-emerald-500 hover:bg-emerald-400 disabled:opacity-50 grid place-items-center text-2xl shadow-lg shadow-emerald-900/40 animate-bounce"
            >
              📞
            </button>
            <span className="text-xs text-white/60">Accept</span>
          </div>
        </div>

        <p className="mt-10 text-[11px] text-white/35">
          Demo: you are the customer. Decline, or let it ring out, and the system records a missed call and plans a
          call-back.
        </p>
      </div>
    </div>
  );
}

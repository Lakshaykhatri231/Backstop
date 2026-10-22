import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api/client";
import type { OtpSendResult } from "@/lib/api/contact";

// Phone number + OTP check, used at registration, on the storefront profile
// card and (in a simpler fixed-number form) on the self-serve page. The widget
// never decides anything: it asks the backend to send a code and to check it,
// and tells its parent when - and for which number - the check passed.

type Variant = "dark" | "light";

function errMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { detail?: unknown } | null;
    const d = body?.detail;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    if (typeof d === "string") return d;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}

export function PhoneVerify({
  send,
  verify,
  onVerified,
  onReset,
  purposeLine,
  variant = "light",
  initialPhone = "",
  disabled = false,
}: {
  send: (phone: string) => Promise<OtpSendResult>;
  /** Resolves on success (optionally returning a proof token); throws on a wrong code. */
  verify: (phone: string, code: string) => Promise<string | void>;
  onVerified: (phone: string, proof: string | undefined) => void;
  /** Called when a verified number is edited, so the parent drops its proof. */
  onReset?: () => void;
  purposeLine?: string | undefined;
  variant?: Variant;
  initialPhone?: string;
  disabled?: boolean;
}) {
  const [phone, setPhone] = useState(initialPhone);
  const [code, setCode] = useState("");
  const [stage, setStage] = useState<"enter" | "sent" | "verified">("enter");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [devCode, setDevCode] = useState<string | null>(null);

  const dark = variant === "dark";
  const inputCls = dark
    ? "bg-cream/10 border-cream/20 text-cream placeholder:text-cream/40 focus-visible:ring-tangerine"
    : "";
  const labelCls = dark ? "text-cream/60" : "text-ink/50";
  const noteCls = dark ? "text-cream/60" : "text-ink/50";
  const btnCls = dark
    ? "bg-cream/15 text-cream hover:bg-cream/25"
    : "bg-ink text-cream hover:bg-ink/90";

  async function sendCode() {
    setBusy(true);
    setError(null);
    try {
      const r = await send(phone);
      setDevCode(r.simulated && r.dev_code ? r.dev_code : null);
      setStage("sent");
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function checkCode() {
    setBusy(true);
    setError(null);
    try {
      const proof = await verify(phone, code);
      setStage("verified");
      setDevCode(null);
      onVerified(phone, proof ?? undefined);
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function editPhone(v: string) {
    setPhone(v);
    if (stage !== "enter") {
      setStage("enter");
      setCode("");
      setDevCode(null);
      onReset?.();
    }
  }

  return (
    <div className="space-y-2">
      <label className={`block text-xs uppercase tracking-[0.15em] ${labelCls}`}>Mobile number</label>
      <div className="flex gap-2">
        <Input
          inputMode="tel"
          autoComplete="tel"
          placeholder="98765 43210"
          value={phone}
          disabled={disabled}
          onChange={(e) => editPhone(e.target.value)}
          className={inputCls}
        />
        {stage !== "verified" && (
          <Button
            type="button"
            disabled={disabled || busy || phone.trim().length < 10}
            onClick={sendCode}
            className={`shrink-0 ${btnCls}`}
          >
            {busy && stage === "enter" ? "Sending…" : stage === "sent" ? "Resend" : "Send code"}
          </Button>
        )}
        {stage === "verified" && (
          <span className="shrink-0 self-center text-sm font-semibold text-soft">✓ Verified</span>
        )}
      </div>

      {stage === "sent" && (
        <div className="space-y-2">
          <div className="flex gap-2">
            <Input
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              placeholder="6-digit code"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              className={inputCls}
            />
            <Button
              type="button"
              disabled={busy || code.length !== 6}
              onClick={checkCode}
              className="shrink-0 bg-tangerine text-ink hover:bg-tangerine/90"
            >
              {busy ? "Checking…" : "Verify"}
            </Button>
          </div>
          {devCode && (
            <p className={`text-xs ${noteCls}`}>
              Demo mode — no SMS is sent. Your code is{" "}
              <button type="button" className="font-mono font-bold underline" onClick={() => setCode(devCode)}>
                {devCode}
              </button>
              .
            </p>
          )}
        </div>
      )}

      {purposeLine && <p className={`text-[11px] ${noteCls}`}>{purposeLine}</p>}
      {error && <p className="text-sm text-failed">{error}</p>}
    </div>
  );
}

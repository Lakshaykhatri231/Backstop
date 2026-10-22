import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api/client";
import {
  getSelfServeContext,
  LANGUAGE_LABEL,
  openSelfServePayLink,
  sendSelfServeOtp,
  submitSelfServe,
  type Language,
  type SelfServeRef,
} from "@/lib/api/contact";

function errMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: unknown } | null)?.detail;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    if (typeof d === "string") return d;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}

const inr = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;

// The customer's own page for ONE debt, reached from a signed link (no
// login - the link is the credential). One visit can: confirm their number by
// a code, say when they will pay, pick a language and agree (or not) to a
// phone call. Consent is only possible once the number is verified.
export function PayLaterPage({ link }: { link: SelfServeRef }) {
  const qc = useQueryClient();
  const key = ["self-serve", link.subject_type, link.subject_id] as const;
  const ctxQ = useQuery({ queryKey: key, queryFn: () => getSelfServeContext(link), retry: false });

  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [devCode, setDevCode] = useState<string | null>(null);
  const [date, setDate] = useState("");
  const [language, setLanguage] = useState<Language | null>(null);
  const [consent, setConsent] = useState<boolean | null>(null);
  const [memory, setMemory] = useState<boolean | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // "Pay now" makes (or reuses) the debt's Razorpay link only when pressed.
  const payNow = useMutation({
    mutationFn: () => openSelfServePayLink(link),
    onSuccess: (r) => {
      if (r.is_stub) {
        setMessage({ kind: "err", text: "Online payment isn't available for this right now. Please choose a date below." });
      } else {
        window.location.href = r.url;
      }
    },
    onError: (e) => setMessage({ kind: "err", text: errMessage(e) }),
  });

  const sendCode = useMutation({
    mutationFn: () => sendSelfServeOtp(link),
    onSuccess: (r) => {
      setCodeSent(true);
      setDevCode(r.simulated && r.dev_code ? r.dev_code : null);
      setMessage(null);
    },
    onError: (e) => setMessage({ kind: "err", text: errMessage(e) }),
  });

  const verify = useMutation({
    mutationFn: () => submitSelfServe(link, { otp_code: code }),
    onSuccess: async () => {
      setMessage({ kind: "ok", text: "Number verified." });
      setCode("");
      await qc.invalidateQueries({ queryKey: key });
    },
    onError: (e) => setMessage({ kind: "err", text: errMessage(e) }),
  });

  const save = useMutation({
    mutationFn: () =>
      submitSelfServe(link, {
        ...(date ? { promised_date: date } : {}),
        ...(language !== null ? { preferred_language: language } : {}),
        ...(consent !== null ? { voice_consent: consent } : {}),
        ...(memory !== null ? { memory_consent: memory } : {}),
      }),
    onSuccess: async (r) => {
      const parts: string[] = [];
      if (r.promise) parts.push(`Thank you - we have noted that you will pay on ${r.promise.promised_date}.`);
      if (r.promise_error) parts.push(r.promise_error.reason);
      if (r.consent_error) parts.push(r.consent_error.message);
      if (!r.promise && !r.promise_error && !r.consent_error) parts.push("Saved.");
      setMessage({ kind: r.promise_error || r.consent_error ? "err" : "ok", text: parts.join(" ") });
      await qc.invalidateQueries({ queryKey: key });
    },
    onError: (e) => setMessage({ kind: "err", text: errMessage(e) }),
  });

  if (ctxQ.isLoading) {
    return <Shell>Loading…</Shell>;
  }
  if (ctxQ.isError || !ctxQ.data) {
    return (
      <Shell>
        <p className="font-semibold">This link isn't valid.</p>
        <p className="text-sm text-ink/60 mt-1">It may have expired or been copied incorrectly. Please contact the merchant.</p>
      </Shell>
    );
  }

  const ctx = ctxQ.data;
  const c = ctx.contact;
  const effLanguage = language ?? c.preferred_language;
  const effConsent = consent ?? c.voice_consent;
  // Someone who agreed to the older "short notes" wording sees the box un-ticked until they agree to the new one.
  const effMemory = memory ?? !!(c.memory_consent && c.memory_consent_current !== false);

  if (!ctx.is_open) {
    return (
      <Shell>
        <p className="font-semibold">All settled - thank you.</p>
        <p className="text-sm text-ink/60 mt-1">There is nothing left to pay for {ctx.label}.</p>
      </Shell>
    );
  }

  return (
    <Shell>
      <h1 className="font-display text-2xl font-semibold tracking-tight">Choose when you'll pay</h1>
      <p className="text-sm text-ink/60 mt-1">
        {ctx.label}
        {ctx.merchant_for ? ` · ${ctx.merchant_for}` : ""} · <span className="font-semibold">{inr(ctx.amount_inr)}</span> pending
      </p>

      <Button type="button" className="mt-4 w-full" disabled={payNow.isPending} onClick={() => payNow.mutate()}>
        {payNow.isPending ? "Opening Razorpay…" : `Pay ${inr(ctx.amount_inr)} now`}
      </Button>
      <p className="text-[11px] text-ink/50 mt-1 text-center">Secure payment on Razorpay. Or pick a date below.</p>

      {ctx.open_promise_date && (
        <div className="mt-4 rounded-lg bg-soft/10 text-soft px-3 py-2 text-sm">
          You've told us you'll pay on <strong>{ctx.open_promise_date}</strong>. You can change this by choosing a new date.
        </div>
      )}

      {/* 1. number */}
      <section className="mt-6">
        <h2 className="text-xs uppercase tracking-[0.15em] text-ink/50 mb-2">Your mobile number</h2>
        {!c.has_phone ? (
          <p className="text-sm text-ink/60">
            We don't have a number on file for you. You can still choose a date below; to also get help by phone, please
            contact the merchant to add your number.
          </p>
        ) : c.phone_verified ? (
          <p className="text-sm">
            {c.phone_masked} <span className="text-soft font-semibold">✓ verified</span>
          </p>
        ) : (
          <div className="space-y-2">
            <p className="text-sm">
              {c.phone_masked}{" "}
              <span className="text-ink/50">- we'll text a code to confirm this is your number.</span>
            </p>
            <Button type="button" onClick={() => sendCode.mutate()} disabled={sendCode.isPending}>
              {sendCode.isPending ? "Sending…" : codeSent ? "Resend code" : "Send code"}
            </Button>
            {codeSent && (
              <div className="flex gap-2 pt-1">
                <Input
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  placeholder="6-digit code"
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                />
                <Button type="button" onClick={() => verify.mutate()} disabled={code.length !== 6 || verify.isPending}>
                  {verify.isPending ? "Checking…" : "Verify"}
                </Button>
              </div>
            )}
            {devCode && (
              <p className="text-xs text-ink/50">
                Demo mode - no SMS is sent. Your code is{" "}
                <button type="button" className="font-mono font-bold underline" onClick={() => setCode(devCode)}>
                  {devCode}
                </button>
                .
              </p>
            )}
          </div>
        )}
      </section>

      {/* 2. date */}
      <section className="mt-6">
        <h2 className="text-xs uppercase tracking-[0.15em] text-ink/50 mb-2">When will you pay?</h2>
        <Input type="date" value={date} min={ctx.min_date} max={ctx.max_date} onChange={(e) => setDate(e.target.value)} />
        <p className="text-[11px] text-ink/40 mt-1">
          Between {ctx.min_date} and {ctx.max_date}.
        </p>
      </section>

      {/* 3. language + consent */}
      <section className="mt-6 space-y-3">
        <h2 className="text-xs uppercase tracking-[0.15em] text-ink/50">If we call you</h2>
        <div className="flex items-center gap-3 text-sm">
          <label htmlFor="lang" className="text-ink/60">
            Language
          </label>
          <select
            id="lang"
            value={effLanguage}
            onChange={(e) => setLanguage(e.target.value as Language)}
            className="rounded-md border border-ink/15 bg-white px-2 py-1"
          >
            {(Object.keys(LANGUAGE_LABEL) as Language[]).map((l) => (
              <option key={l} value={l}>
                {LANGUAGE_LABEL[l]}
              </option>
            ))}
          </select>
        </div>
        <label className={`flex items-start gap-2.5 text-sm leading-relaxed ${c.phone_verified ? "" : "text-ink/40"}`}>
          <input
            type="checkbox"
            className="mt-1"
            checked={effConsent}
            disabled={!c.phone_verified}
            onChange={(e) => setConsent(e.target.checked)}
          />
          <span>
            {ctx.consent_text.text}
            {!c.phone_verified && <span className="block text-[11px]">Verify your number above to enable this.</span>}
          </span>
        </label>
        <label className={`flex items-start gap-2.5 text-sm leading-relaxed ${c.phone_verified ? "" : "text-ink/40"}`}>
          <input
            type="checkbox"
            className="mt-1"
            checked={effMemory}
            disabled={!c.phone_verified}
            onChange={(e) => setMemory(e.target.checked)}
          />
          <span>
            {ctx.memory_consent_text ?? "Remember our calls so future calls are easier: keep short notes and a record of what was said. You can have it all deleted at any time."}
            <span className="block text-[11px] text-ink/50">Un-tick and save to have them deleted.</span>
          </span>
        </label>
      </section>

      <Button
        type="button"
        className="mt-6 w-full"
        disabled={save.isPending || (!date && language === null && consent === null && memory === null)}
        onClick={() => save.mutate()}
      >
        {save.isPending ? "Saving…" : "Save"}
      </Button>

      {message && (
        <p className={`mt-3 text-sm ${message.kind === "ok" ? "text-soft" : "text-failed"}`}>{message.text}</p>
      )}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <div className="mx-auto max-w-md px-4 py-10">
        <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
          {children}
        </div>
        <p className="text-center text-[11px] text-ink/40 mt-4">Backstop · secure payment help</p>
      </div>
    </div>
  );
}

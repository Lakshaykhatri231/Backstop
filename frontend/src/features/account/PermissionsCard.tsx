import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { PhoneVerify } from "@/features/auth/PhoneVerify";
import type { ConsentTexts, Note, PermissionsInput, PermissionsResult } from "@/lib/api/account";
import { ApiError } from "@/lib/api/client";
import { LANGUAGE_LABEL, type ContactStatus, type Language, type OtpSendResult } from "@/lib/api/contact";

export function errMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: unknown } | null)?.detail;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    if (typeof d === "string") return d;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}

const toneCls: Record<Note["tone"], string> = {
  ok: "border-soft/40 bg-soft/10 text-ink",
  info: "border-ink/10 bg-sand/40 text-ink/80",
  warn: "border-declined/50 bg-declined/10 text-ink",
};

export function NotesList({ notes }: { notes: Note[] }) {
  if (!notes.length) return null;
  return (
    <ul className="space-y-2">
      {notes.map((n, i) => (
        <li key={i} className={`rounded-lg border px-3 py-2 text-sm leading-relaxed ${toneCls[n.tone]}`}>
          {n.text}
        </li>
      ))}
    </ul>
  );
}

// The permissions we ask for - verified number, calls, language, memory - on
// the customer's own page. Nothing is pre-ticked and nothing here can tick a
// box for someone else: the server records consent only from this person's
// own request, and only once their number is verified.
export function PermissionsCard({
  contact,
  consentText,
  notes,
  save,
  sendOtp,
  verifyPhone,
  onChanged,
  intro,
}: {
  contact: ContactStatus;
  consentText: ConsentTexts | undefined;
  notes: Note[];
  save: (body: PermissionsInput) => Promise<PermissionsResult>;
  sendOtp: (phone: string) => Promise<OtpSendResult>;
  verifyPhone: (phone: string, code: string) => Promise<unknown>;
  onChanged: () => void;
  intro?: string;
}) {
  const [language, setLanguage] = useState<Language>(contact.preferred_language);
  const [voice, setVoice] = useState(contact.voice_consent);
  const [memory, setMemory] = useState(!!contact.memory_consent && contact.memory_consent_current !== false);
  const [shownNotes, setShownNotes] = useState<Note[] | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // follow the server after a verify / reload
  useEffect(() => {
    setLanguage(contact.preferred_language);
    setVoice(contact.voice_consent);
    setMemory(!!contact.memory_consent && contact.memory_consent_current !== false);
  }, [contact.preferred_language, contact.voice_consent, contact.memory_consent, contact.memory_consent_current]);

  const mut = useMutation({
    mutationFn: () => save({ preferred_language: language, voice_consent: voice, memory_consent: memory }),
    onSuccess: (r) => {
      setShownNotes(r.notes);
      setMsg({ ok: true, text: "Saved." });
      onChanged();
    },
    onError: (e) => setMsg({ ok: false, text: errMessage(e) }),
  });

  const verified = contact.phone_verified;
  return (
    <div className="space-y-5">
      {intro && <p className="text-sm text-ink/60">{intro}</p>}

      <section>
        <h3 className="text-xs uppercase tracking-[0.15em] text-ink/50 mb-2">1 · Your mobile number</h3>
        {verified ? (
          <p className="text-sm">
            {contact.phone_masked} <span className="text-soft font-semibold">✓ verified</span>
          </p>
        ) : (
          <div className="space-y-2">
            <p className="text-sm text-ink/60">
              {contact.has_phone
                ? `We have ${contact.phone_masked} on file. Enter your number and we will text you a code to confirm it is yours.`
                : "Add your number and we will text you a code to confirm it."}
            </p>
            <PhoneVerify
              send={sendOtp}
              verify={async (p, c) => {
                await verifyPhone(p, c);
              }}
              onVerified={() => onChanged()}
            />
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="text-xs uppercase tracking-[0.15em] text-ink/50">2 · If we need to reach you</h3>
        <div className="flex items-center gap-3 text-sm">
          <label htmlFor="perm-lang" className="text-ink/60">
            Language
          </label>
          <select
            id="perm-lang"
            value={language}
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
        <label className={`flex items-start gap-2.5 text-sm leading-relaxed ${verified ? "" : "text-ink/40"}`}>
          <input type="checkbox" className="mt-1" checked={voice} disabled={!verified} onChange={(e) => setVoice(e.target.checked)} />
          <span>
            {consentText?.voice.text ?? "You may call me on this number about a payment that failed or is overdue."}
            {!verified && <span className="block text-[11px]">Verify your number above to enable this.</span>}
          </span>
        </label>
        <label className={`flex items-start gap-2.5 text-sm leading-relaxed ${verified ? "" : "text-ink/40"}`}>
          <input type="checkbox" className="mt-1" checked={memory} disabled={!verified} onChange={(e) => setMemory(e.target.checked)} />
          <span>
            {consentText?.memory.text ?? "Remember our calls so future calls are easier."}
            <span className="block text-[11px] text-ink/50">Un-tick and save to have them deleted.</span>
          </span>
        </label>
        <Button type="button" onClick={() => mut.mutate()} disabled={mut.isPending}>
          {mut.isPending ? "Saving…" : "Save my choices"}
        </Button>
        {msg && <p className={`text-sm ${msg.ok ? "text-soft" : "text-failed"}`}>{msg.text}</p>}
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-[0.15em] text-ink/50 mb-2">What this means for you</h3>
        <NotesList notes={shownNotes ?? notes} />
      </section>
    </div>
  );
}

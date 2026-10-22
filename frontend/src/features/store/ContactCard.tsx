import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { PhoneVerify } from "@/features/auth/PhoneVerify";
import {
  getConsentText,
  LANGUAGE_LABEL,
  sendProfileOtp,
  updateMyContact,
  verifyProfilePhone,
  type ContactStatus,
  type Language,
} from "@/lib/api/contact";
import { authMeKey } from "@/lib/hooks/useAuth";

// "Calls about a failed payment" - the customer's own control over their
// number and consent. Consent can only be ticked for a verified number
// (the backend enforces it too), and unticking is always allowed.
export function ContactCard({ contact }: { contact: ContactStatus }) {
  const qc = useQueryClient();
  const consentQ = useQuery({ queryKey: ["contacts", "consent-text"], queryFn: getConsentText, staleTime: Infinity });
  const [changing, setChanging] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: authMeKey });

  const update = useMutation({
    mutationFn: updateMyContact,
    onSuccess: refresh,
  });

  const needsPhone = !contact.phone_verified;

  return (
    <div className="rounded-2xl bg-white border border-ink/5 p-5 mb-4 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
      <h2 className="font-display font-semibold text-base mb-1">Payment help by phone</h2>

      {needsPhone || changing ? (
        <div className="mt-2">
          <p className="text-xs text-ink/50 mb-3">
            {contact.has_phone && !contact.phone_verified
              ? `Verify ${contact.phone_masked} to receive help with a failed payment.`
              : "Add and verify your mobile number so we can help if a payment fails."}
          </p>
          <PhoneVerify
            purposeLine={consentQ.data?.phone_purpose}
            send={sendProfileOtp}
            verify={async (phone, code) => {
              await verifyProfilePhone(phone, code);
            }}
            onVerified={() => {
              setChanging(false);
              refresh();
            }}
          />
          {changing && (
            <button type="button" className="mt-2 text-xs text-ink/50 underline" onClick={() => setChanging(false)}>
              Cancel
            </button>
          )}
        </div>
      ) : (
        <div className="mt-2 space-y-3">
          <div className="flex items-center justify-between text-sm">
            <span>
              {contact.phone_masked} <span className="text-soft font-semibold">✓ verified</span>
            </span>
            <button type="button" className="text-xs text-ink/50 underline" onClick={() => setChanging(true)}>
              Change number
            </button>
          </div>

          <div className="flex items-center gap-3 text-sm">
            <label htmlFor="pref-lang" className="text-ink/60">
              Language
            </label>
            <select
              id="pref-lang"
              value={contact.preferred_language}
              disabled={update.isPending}
              onChange={(e) => update.mutate({ preferred_language: e.target.value as Language })}
              className="rounded-md border border-ink/15 bg-white px-2 py-1 text-sm"
            >
              {(Object.keys(LANGUAGE_LABEL) as Language[]).map((l) => (
                <option key={l} value={l}>
                  {LANGUAGE_LABEL[l]}
                </option>
              ))}
            </select>
          </div>

          <label className="flex items-start gap-2.5 text-xs leading-relaxed text-ink/70">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={contact.voice_consent}
              disabled={update.isPending}
              onChange={(e) => update.mutate({ voice_consent: e.target.checked })}
            />
            <span>{consentQ.data?.text}</span>
          </label>
          <p className="text-[11px] text-ink/40">
            {contact.voice_consent
              ? "We may call you between 8am and 7pm about a failed payment."
              : "We will not call you. You can turn this on any time."}
          </p>
          {update.error && <p className="text-xs text-failed">{update.error.message}</p>}
        </div>
      )}
    </div>
  );
}

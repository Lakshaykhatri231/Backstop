import { apiFetch } from "./client";

// ── Shapes shared by registration, the profile card and the self-serve page ──

export type ContactStatus = {
  has_phone: boolean;
  phone_masked: string;
  phone_verified: boolean;
  preferred_language: "en" | "hi" | "hinglish";
  voice_consent: boolean;
  memory_consent?: boolean;
  memory_consent_current?: boolean;   // false for someone who agreed to the older "short notes" wording
  consent_at?: string | null;
  can_call: boolean;
  block_reason: string | null;
};

export type ConsentText = { version: string; text: string; phone_purpose: string };

export type OtpSendResult = {
  sent: boolean;
  expires_in: number;
  simulated: boolean;
  /** Present ONLY when the backend OTP provider is the simulated one. */
  dev_code?: string;
};

export type Language = "en" | "hi" | "hinglish";

// Hinglish is the default and is listed first; the customer can switch to
// Hindi or English wherever language is shown.
export const DEFAULT_LANGUAGE: Language = "hinglish";

export const LANGUAGE_LABEL: Record<Language, string> = {
  hinglish: "Hinglish",
  hi: "हिन्दी (Hindi)",
  en: "English",
};

export const getConsentText = () => apiFetch<ConsentText>("/contacts/consent-text", { auth: false });

// ── Pre-registration phone verification (public) ──
export const sendRegisterOtp = (phone: string) =>
  apiFetch<OtpSendResult>("/auth/phone/otp/send", { method: "POST", body: { phone }, auth: false });

export const verifyRegisterOtp = (phone: string, code: string) =>
  apiFetch<{ phone: string; phone_proof: string }>("/auth/phone/otp/verify", {
    method: "POST",
    body: { phone, code },
    auth: false,
  });

// ── Logged-in customer's own contact ──
export const getMyContact = () => apiFetch<ContactStatus>("/me/contact");

export const sendProfileOtp = (phone: string) =>
  apiFetch<OtpSendResult>("/me/contact/otp/send", { method: "POST", body: { phone } });

export const verifyProfilePhone = (phone: string, code: string) =>
  apiFetch<ContactStatus>("/me/contact/verify", { method: "POST", body: { phone, code } });

export const updateMyContact = (body: { preferred_language?: Language; voice_consent?: boolean }) =>
  apiFetch<ContactStatus>("/me/contact", { method: "PUT", body });

// ── Self-serve page (signed link, no login) ──
export type SelfServeRef = { subject_type: string; subject_id: string; token: string };

export type SelfServeContext = {
  label: string;
  ref: string;
  merchant_for?: string;
  subject_type: string;
  amount_inr: number;
  is_open: boolean;
  min_date: string;
  max_date: string;
  open_promise_date: string | null;
  contact: ContactStatus;
  consent_text: { version: string; text: string };
  memory_consent_text?: string;
};

export type SelfServeSubmitResult = {
  verified: boolean;
  consent: boolean;
  consent_error?: { code: string; message: string };
  promise: { id: string; promised_date: string; amount_inr: number } | null;
  promise_error: { code: string; reason: string } | null;
  contact: ContactStatus;
};

export const getSelfServeContext = (r: SelfServeRef) =>
  apiFetch<SelfServeContext>(
    `/self-serve/context?subject_type=${encodeURIComponent(r.subject_type)}` +
      `&subject_id=${encodeURIComponent(r.subject_id)}&token=${encodeURIComponent(r.token)}`,
    { auth: false },
  );

export const openSelfServePayLink = (r: SelfServeRef) =>
  apiFetch<{ url: string; is_stub: boolean; amount_inr: number }>("/self-serve/pay-link", {
    method: "POST", body: r, auth: false,
  });

export const sendSelfServeOtp = (r: SelfServeRef) =>
  apiFetch<OtpSendResult>("/self-serve/otp/send", { method: "POST", body: r, auth: false });

export const submitSelfServe = (
  r: SelfServeRef,
  body: { otp_code?: string; promised_date?: string; preferred_language?: Language; voice_consent?: boolean; memory_consent?: boolean },
) => apiFetch<SelfServeSubmitResult>("/self-serve/submit", { method: "POST", body: { ...r, ...body }, auth: false });

// ── Merchant dashboard: number + language only (never consent) ──
export type MerchantContact = ContactStatus & {
  id: string;
  razorpay_customer_id: string | null;
  customer_id: string | null;
  business_account_id: string | null;
};

export const getContacts = () => apiFetch<MerchantContact[]>("/contacts", { auth: false });

export const setContact = (body: {
  razorpay_customer_id?: string;
  business_account_id?: string;
  phone: string;
  preferred_language: Language;
}) => apiFetch<MerchantContact>("/contacts", { method: "POST", body, auth: false });

export type MandateRunRow = {
  id: string;
  subscription_id: string;
  razorpay_customer_id: string | null;
  amount_inr: number;
  status: string;
  attempts_made: number;
  next_action_at: string | null;
  opened_at: string;
  contact: ContactStatus;
  self_serve_path: string;
};

export const getMandateRuns = () => apiFetch<MandateRunRow[]>("/merchant/mandate-runs", { auth: false });

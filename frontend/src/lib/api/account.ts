import { apiFetch } from "./client";
import type { ContactStatus, Language, OtpSendResult } from "./contact";

// ── Shared: permission notes + consent wording ──

export type Note = { tone: "ok" | "info" | "warn"; text: string };
export type ConsentTexts = {
  voice: { version: string; text: string; phone_purpose: string };
  memory: { version: string; text: string };
};
export type PermissionsInput = {
  preferred_language?: Language;
  voice_consent?: boolean;
  memory_consent?: boolean;
};
export type PermissionsResult = { contact: ContactStatus; notes: Note[] };

// ── Customer: autopay ──

export type Service = { key: string; name: string; amount_inr: number; frequency: string; blurb: string };
export type ServicesResponse = {
  services: Service[];
  custom: { key: string; min_inr: number; max_inr: number; frequencies: string[] };
  test_mode: boolean;
  consent_text: ConsentTexts;
};

export type AutopayDebit = { at: string; status: "paid" | "in progress" | "refused"; amount_inr: number | null };

export type Autopay = {
  id: string;
  status: "setup" | "active" | "rejected" | "cancelled" | "paused";
  service_key: string | null;
  service_name: string;
  amount_inr: number;
  frequency: string;
  registration_url: string | null;
  next_debit_at: string | null;
  next_debit_amount_inr: number | null;
  notice_at: string | null;
  history: AutopayDebit[];
  created_at: string;
  contact: ContactStatus;
  notes: Note[];
};

export const getServices = () => apiFetch<ServicesResponse>("/me/autopay/services", { auth: false });
export const listMyAutopay = () => apiFetch<Autopay[]>("/me/autopay");
export const startAutopay = (body: { service_key: string; amount_inr?: number; frequency?: string }) =>
  apiFetch<Autopay>("/me/autopay", { method: "POST", body });
export const syncAutopay = (id: string) => apiFetch<Autopay>(`/me/autopay/${id}/sync`, { method: "POST" });
export const cancelAutopay = (id: string) => apiFetch<Autopay>(`/me/autopay/${id}/cancel`, { method: "POST" });
export const saveAutopayPermissions = (body: PermissionsInput) =>
  apiFetch<PermissionsResult>("/me/autopay/permissions", { method: "PUT", body });

// ── Business customer: invoices + permissions ──

export type BizInvoice = {
  id: string;
  invoice_no: string;
  amount_inr: number;
  amount_paid: number;
  outstanding_inr: number;
  status: string;
  due_at: string;
  days_past_due: number;
  paid_at: string | null;
};

export type BizOverview = {
  account: { id: string; email: string; name: string; company_name: string | null };
  invoices: BizInvoice[];
  contact: ContactStatus;
  notes: Note[];
  consent_text: ConsentTexts;
};

export const getBizOverview = () => apiFetch<BizOverview>("/biz/invoices");
export const bizPayLink = (invoiceId: string) =>
  apiFetch<{ url: string; is_stub: boolean; amount_inr: number }>(`/biz/invoices/${invoiceId}/pay-link`, { method: "POST" });
export const bizSendOtp = (phone: string) =>
  apiFetch<OtpSendResult>("/biz/contact/otp/send", { method: "POST", body: { phone } });
export const bizVerifyPhone = async (phone: string, code: string) => {
  await apiFetch<PermissionsResult>("/biz/contact/verify", { method: "POST", body: { phone, code } });
};
export const saveBizPermissions = (body: PermissionsInput) =>
  apiFetch<PermissionsResult>("/biz/contact", { method: "PUT", body });

// ── Merchant: business customers + invoices ──

export type BusinessCustomer = {
  id: string;
  company_name: string;
  contact_name: string | null;
  email: string | null;
  gstin: string | null;
  payment_terms_days: number;
  phone: string | null;
  login_email: string | null;
  phone_masked: string;
  phone_verified: boolean;
  can_call: boolean;
  preferred_language: Language;
};

export type NewBusinessCustomer = {
  company_name: string;
  contact_name?: string;
  email: string;
  phone?: string;
  preferred_language?: Language;
};

export const listBusinessCustomers = () => apiFetch<BusinessCustomer[]>("/merchant/business-customers");
export const createBusinessCustomer = (body: NewBusinessCustomer) =>
  apiFetch<{ account_id: string; company_name: string; login_email: string; temporary_password: string }>(
    "/merchant/business-customers",
    { method: "POST", body },
  );
export const registerInvoice = (body: { account_id: string; amount_inr: number; due_at: string; invoice_no?: string }) =>
  apiFetch<{ id: string; invoice_no: string; amount_inr: number; due_at: string }>("/merchant/invoices", {
    method: "POST",
    body,
  });

export type BusinessCustomerPatch = Partial<{
  company_name: string; contact_name: string; email: string; phone: string;
  preferred_language: Language; gstin: string; payment_terms_days: number;
}>;
export const updateBusinessCustomer = (id: string, body: BusinessCustomerPatch) =>
  apiFetch<{ id: string; updated: string[]; verification_and_consent_cleared: boolean }>(
    `/merchant/business-customers/${id}`, { method: "PUT", body });
export const resetBusinessPassword = (id: string) =>
  apiFetch<{ login_email: string; temporary_password: string }>(
    `/merchant/business-customers/${id}/reset-password`, { method: "POST" });

import { apiFetch } from "./client";

export type AgingBucket = { amount_inr: number; invoices: number };

export type ReceivablesSummary = {
  aging: Record<"0-30" | "31-60" | "61-90" | "90+", AgingBucket>;
  not_yet_due_inr: number;
  overdue_inr: number;
  collected_inr: number;
  written_off_inr: number;
  avg_days_to_collect: number | null;
  disputed: number;
};

export type Invoice = {
  id: string;
  invoice_no: string;
  company_name: string | null;
  amount_inr: number;
  amount_paid: number;
  outstanding_inr: number;
  status: string;
  due_at: string;
  days_past_due: number;
  escalated_at: string | null;
  self_serve_path: string;
};

// ── CSV import (the browser parses; these two calls preview and commit) ──
export type ImportRow = {
  invoice_no: string;
  amount_inr: number;
  company_name?: string;
  gstin?: string;
  issued_at?: string;
  due_at?: string;
};

export type ImportPreviewRow = {
  invoice_no: string;
  amount_inr: number;
  status: "new" | "exists" | "rejected" | "duplicate_in_file";
  account: "matched" | "new_account" | "unmatched";
  company_name: string | null;
  problems: string[];
};

export type ImportPreview = {
  rows: ImportPreviewRow[];
  counts: { new: number; exists: number; rejected: number; duplicate_in_file: number };
  new_accounts: string[];
  total: number;
};

export type ImportResult = {
  results: { invoice_no: string; status: string; problems: string[]; id?: string }[];
  accounts_created: string[];
  counts: ImportPreview["counts"];
  total: number;
};

export const IMPORT_ROW_CAP = 2000;

export const previewInvoiceImport = (rows: ImportRow[]) =>
  apiFetch<ImportPreview>("/receivables/invoices/import-preview", {
    method: "POST",
    body: { rows },
    auth: false,
  });

export const commitInvoiceImport = (rows: ImportRow[], createMissingAccounts: boolean) =>
  apiFetch<ImportResult>("/receivables/invoices/import", {
    method: "POST",
    body: { rows, create_missing_accounts: createMissingAccounts },
    auth: false,
  });

export type BusinessAccount = {
  id: string;
  company_name: string;
  gstin: string | null;
  phone_masked: string;
  phone_verified: boolean;
  preferred_language: string;
  voice_consent: boolean;
  can_call: boolean;
};

export const getAccounts = () => apiFetch<BusinessAccount[]>("/receivables/accounts", { auth: false });

export type Promise_ = {
  id: string;
  subject_type: string;
  subject_id: string;
  amount_inr: number;
  promised_date: string;
  captured_via: string;
  status: string;
  amount_paid: number;
  state: "waiting" | "due_today" | "in_grace" | "kept" | "partly_kept" | "broken" | "cancelled";
  days_left: number;
  check_on: string;
  label: string;
};

export type PromisesResponse = {
  promises: Promise_[];
  counts: Record<string, number>;
  kept_rate_by_source: Record<string, { resolved: number; kept: number; kept_rate: number }>;
};

export type VoiceCall = {
  id: string;
  subject_type: string;
  subject_id: string;
  language: string;
  status: string;
  transcript: string | null;
  intent: string | null;
  intent_source: string | null;
  routed_to: string | null;
  created_at: string;
};

export const getReceivablesSummary = () => apiFetch<ReceivablesSummary>("/receivables/summary", { auth: false });
export const getInvoices = () => apiFetch<Invoice[]>("/receivables/invoices", { auth: false });
export const getPromises = () => apiFetch<PromisesResponse>("/merchant/promises", { auth: false });
export const getVoiceCalls = () => apiFetch<VoiceCall[]>("/voice/calls", { auth: false });

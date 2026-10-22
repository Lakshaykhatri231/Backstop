import { apiFetch } from "./client";

// Demo Console API (/demo/*). The backend 404s all of this in production.

export type DemoClock = {
  now_utc: string;
  now_ist: string;
  offset_hours: number;
  shifted: boolean;
  in_contact_window: boolean;
};

export type DemoState = {
  environment: string;
  clock: DemoClock;
  providers: { otp: string; messaging: string; voice: string; dnd: string };
  interactive_calls: boolean;
  scheduled_pending: number;
  messages_total: number;
  ringing_calls: number;
  modes?: DemoModes;
};

export type DemoMessage = {
  id: string;
  template: string;
  subject_type: string | null;
  subject_id: string | null;
  to_phone: string | null;
  language: string;
  body: string;
  link: string | null;
  link_kind: "self_serve" | "razorpay_payment_link" | "razorpay_registration_link" | "storefront" | "placeholder" | null;
  provider: string;
  status: "recorded" | "sent" | "failed" | "not_sent_no_contact";
  error: string | null;
  created_at: string;
};

export type DemoContact = {
  has_phone: boolean;
  phone_masked: string;
  phone_verified: boolean;
  preferred_language: string;
  voice_consent: boolean;
  memory_consent?: boolean;
  can_call: boolean;
  block_reason: string | null;
};

export type PromiseState = {
  id: string;
  promised_date: string;
  amount_inr: number;
  status: string;
  amount_paid: number;
  state: "waiting" | "due_today" | "in_grace" | "kept" | "partly_kept" | "broken" | "cancelled";
  days_left: number;
  check_on: string;
  label: string;
  captured_via: string;
};

export type DemoSubject = {
  subject_type: "mandate_run" | "mandate" | "invoice" | "order";
  subject_id: string;
  title: string;
  amount_inr: number;
  status: string;
  settled: boolean;
  contact: DemoContact;
  self_serve_path: string | null;
  open_promise: { id: string; promised_date: string; amount_inr: number } | null;
  promises: PromiseState[];
  next_action_at: string | null;
  created_at: string;
};

export type ScheduledRow = {
  id: string;
  kind: string;
  subject_type: string;
  subject_id: string;
  due_at: string;
  status: string;
  result: string | null;
  executed_at: string | null;
  plain: string;
};

export type TimelineItem = {
  at: string;
  type: "message" | "call" | "promise" | "job";
  title: string;
  detail: string;
  body: string;
  tone: "ok" | "info" | "warn" | "bad";
  raw: string;
};

export type RingingCall = {
  call_id: string;
  subject_type: string;
  subject_id: string;
  script: string;
  language: string;
  caller: string;
  phone_masked: string;
};

export type CallStart =
  | { ok: true; call_id: string; script: string; language: string; phone_masked: string }
  | { ok: false; block: string };

export type CallResult = {
  ok: boolean;
  status: string;
  intent: string;
  intent_source: string;
  promise_date: string | null;
  routed_to: string;
  explanation: string;
  tone: "ok" | "info" | "warn" | "bad";
};

export type TurnTimings = { server_ms?: number; llm_ms?: number; stt_ms?: number; tts_ms?: number; retrieve_ms?: number };
export type AgentLine = {
  turn_no: number; text: string; kind: string | null;
  timings?: TurnTimings | null; sources?: { title: string; heading?: string | null; score: number }[] | null;
};
export type TurnResult =
  | { ok: true; done: false; agent: AgentLine; understood: string | null }
  | (CallResult & { done: true; agent: AgentLine | null });

export type AdvanceResult = {
  moved_hours: number;
  ran: { executed: number; deferred: number; skipped: number; failed: number };
  note: string;
  clock: DemoClock;
};

export const getDemoState = () => apiFetch<DemoState>("/demo/state");
export const getDemoMessages = () => apiFetch<DemoMessage[]>("/demo/messages?limit=150");
export const getDemoSubjects = () => apiFetch<DemoSubject[]>("/demo/subjects");
export const getDemoScheduled = () =>
  apiFetch<{ upcoming: ScheduledRow[]; recent: ScheduledRow[] }>("/demo/scheduled?limit=40");
export const getDemoTimeline = (subject_type: string, subject_id: string) =>
  apiFetch<TimelineItem[]>(
    `/demo/timeline?subject_type=${encodeURIComponent(subject_type)}&subject_id=${encodeURIComponent(subject_id)}`,
  );
export const getRingingCalls = () => apiFetch<RingingCall[]>("/demo/calls/ringing");

export const advanceClock = (hours: number) =>
  apiFetch<AdvanceResult>("/demo/clock/advance", { method: "POST", body: { hours } });
export const nextEvent = (subjectId?: string) =>
  apiFetch<AdvanceResult>(
    `/demo/clock/next${subjectId ? `?subject_id=${encodeURIComponent(subjectId)}` : ""}`,
    { method: "POST" },
  );
export const toWindow = () => apiFetch<AdvanceResult>("/demo/clock/to-window", { method: "POST" });
export const resetClock = () => apiFetch<{ clock: DemoClock }>("/demo/clock/reset", { method: "POST" });
export const setInteractive = (interactive: boolean) =>
  apiFetch<{ interactive_calls: boolean }>("/demo/voice-mode", { method: "POST", body: { interactive } });

export const createMandateScenario = (b: { phone: string; language: string; amount_inr: number; reason: string }) =>
  apiFetch<{ subject_type: string; subject_id: string | null; self_serve_path: string | null }>(
    "/demo/scenarios/mandate",
    { method: "POST", body: b },
  );
export const createInvoiceScenario = (b: { phone: string; language: string; amount_inr: number; days_overdue: number }) =>
  apiFetch<{ subject_type: string; subject_id: string; invoice_no: string; self_serve_path: string }>(
    "/demo/scenarios/invoice",
    { method: "POST", body: b },
  );

export const simulatePayment = (subject_type: string, subject_id: string) =>
  apiFetch<Record<string, unknown>>("/demo/payment", { method: "POST", body: { subject_type, subject_id } });

export const promiseDayPaid = (subject_type: string, subject_id: string) =>
  apiFetch<Record<string, unknown>>("/demo/promise-day-paid", { method: "POST", body: { subject_type, subject_id } });

export type LadderStep = {
  step: number; label: string; status: string; due: string;
  title: string; detail: string; tone: string; hint: string;
};
export type InvoiceLadder = {
  invoice: { invoice_no: string; status: string; settled: boolean; outstanding_inr: number; due: string };
  steps: LadderStep[];
  fired?: number;
};
export const getInvoiceLadder = (subject_id: string) =>
  apiFetch<InvoiceLadder>(`/demo/invoice-ladder?subject_id=${encodeURIComponent(subject_id)}`);
export const playInvoicePipeline = (subject_id: string) =>
  apiFetch<InvoiceLadder>(`/demo/invoice-autoplay?subject_id=${encodeURIComponent(subject_id)}`, { method: "POST" });

export const writeOffInvoice = (invoice_id: string, reason: string) =>
  apiFetch<Record<string, unknown>>(`/receivables/invoices/${encodeURIComponent(invoice_id)}/write-off`, {
    method: "POST", body: { reason },
  });

export const startCall = (subject_type: string, subject_id: string) =>
  apiFetch<CallStart>("/demo/calls/start", { method: "POST", body: { subject_type, subject_id } });
export const replyToCall = (call_id: string, transcript: string) =>
  apiFetch<CallResult>(`/demo/calls/${call_id}/reply`, { method: "POST", body: { transcript } });

export const sendTurn = (call_id: string, transcript: string, opts: { stt_ms?: number; interrupted?: boolean } = {}) =>
  apiFetch<TurnResult>(`/demo/calls/${call_id}/turn`, {
    method: "POST", body: { transcript, stt_ms: opts.stt_ms ?? null, interrupted: opts.interrupted ?? false },
  });

export type MoneyLedger = {
  total_revenue: number;
  at_risk_mandate: number;
  at_risk_failed: number;
  at_risk_soft: number;
  at_risk_declined: number;
  total_at_risk: number;
  total_recovered: number;
  total_lost: number;
  receivables: { overdue: number; collected: number; written_off: number };
};

export type MoneySubject = {
  state: "at_risk" | "recovered" | "lost" | "not_due";
  tone: "ok" | "info" | "warn" | "bad";
  headline: string;
  detail: string;
  amount_inr: number;
  lapse_at: string | null;
  hours_to_lapse: number | null;
};

export type MoneyTrailItem = {
  at: string;
  field: string;
  field_label: string;
  delta: number;
  before: number;
  after: number;
  why: string;
};

export type DemoMoney = { ledger: MoneyLedger; subject: MoneySubject | null; trail: MoneyTrailItem[] };

export const getDemoMoney = (subject_type?: string, subject_id?: string) =>
  apiFetch<DemoMoney>(
    subject_type && subject_id
      ? `/demo/money?subject_type=${encodeURIComponent(subject_type)}&subject_id=${encodeURIComponent(subject_id)}`
      : "/demo/money",
  );

export type SpeechConfig = { tts: "azure" | "browser"; stt: "groq" | "browser"; error?: string };
export const getSpeechConfig = () => apiFetch<SpeechConfig>("/demo/speech/config");

import { getToken } from "@/lib/auth-token";
import { getMerchantKey } from "@/lib/merchant-key";

// Direct fetches (audio) carry the same credentials apiFetch does: the shared
// key, and the merchant's own sign-in.
function keyHeader(): Record<string, string> {
  const k = getMerchantKey();
  const t = getToken();
  return { ...(k ? { "X-Merchant-Key": k } : {}), ...(t ? { Authorization: `Bearer ${t}` } : {}) };
}

/** The call script as audio (cloud voice). Throws with the server's message. */
export async function fetchCallAudio(call_id: string, turn?: number): Promise<Blob> {
  const q = turn ? `&turn=${turn}` : "";
  const res = await fetch(`/demo/speech/tts?call_id=${encodeURIComponent(call_id)}${q}`, {
    method: "POST", headers: keyHeader(),
  });
  if (!res.ok) {
    let msg = `Voice service error ${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* keep default */ }
    throw new Error(msg);
  }
  return res.blob();
}

export type Heard = { text: string; reason?: "too_short" | "silent" | "phantom"; duration_ms?: number };

/** A recorded reply -> text (cloud listening). Too-short / silent clips come back
 * with empty text and a `reason`, not an error. `call_id` biases the spelling of names/numbers. */
export async function transcribeAudio(blob: Blob, language: string, callId?: string): Promise<Heard> {
  const q = `language=${encodeURIComponent(language)}${callId ? `&call_id=${encodeURIComponent(callId)}` : ""}`;
  const res = await fetch(`/demo/speech/stt?${q}`, {
    method: "POST", headers: { ...keyHeader(), "Content-Type": blob.type || "audio/webm" }, body: blob,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.detail) || `Listening service error ${res.status}`);
  return { text: ((data?.text as string) ?? "").trim(), ...(data?.reason ? { reason: data.reason } : {}),
           ...(data?.duration_ms != null ? { duration_ms: data.duration_ms } : {}) };
}

export const fillerUrl = (lang: string, n: number) =>
  `/demo/speech/filler?lang=${encodeURIComponent(lang)}&n=${n}`;

export type MemoryTiers = { notes: number; episodes: number; chunks: number; facts: number; links: number };
export type DemoMemory = {
  contact_id: string | null; memory_consent: boolean; records: string[];
  notes: { id: string; kind: string; text: string; created_at: string; expires_at: string }[];
  retention_days: number;
  memory_consent_current?: boolean;
  tiers?: MemoryTiers;
};
export const getDemoMemory = (subject_type: string, subject_id: string) =>
  apiFetch<DemoMemory>(`/demo/memory?subject_type=${encodeURIComponent(subject_type)}&subject_id=${encodeURIComponent(subject_id)}`);
export const forgetDemoMemory = (subject_type: string, subject_id: string) =>
  apiFetch<{ deleted: number }>("/demo/memory/forget", { method: "POST", body: { subject_type, subject_id } });

export type MemoryFact = {
  id: string; key: string; value: string; status: "active" | "superseded"; confidence: number; importance: number;
  valid_from: string; valid_to: string | null; last_confirmed: string;
};
export type MemoryEpisode = {
  id: string; call_id: string; started_at: string; outcome: string | null; summary: string | null;
  summary_source: "llm" | "rules" | "seed" | null; tone_arc: string | null; key_events: string[]; importance: number;
  chunks: { id: string; seq: number; turn_from: number; turn_to: number; text: string; embedded: boolean }[];
};
export type MemoryExplorerData = {
  contact_id: string | null; memory_consent?: boolean; memory_consent_current?: boolean;
  consent_version?: string | null; current_version?: string; retention_days?: number;
  core_card?: string[]; tiers?: MemoryTiers; facts?: MemoryFact[]; episodes?: MemoryEpisode[];
  working?: { call_id: string; status: string; started_at: string; total_turns: number;
              turns: { turn_no: number; speaker: "agent" | "customer"; text: string; kind: string | null }[] } | null;
  records?: string[];
  notes?: { id: string; kind: string; text: string; created_at: string; expires_at: string }[];
  knowledge?: { docs: number; approved_passages: number };
  memory_budget_chars?: number; half_life_days?: number;
  links?: { relation: string; src: string; dst: string }[];
  recent_turns?: { call_id: string; turn_no: number; kind: string | null; text: string;
    sources: { title: string; heading: string | null; score: number }[] | null;
    memory: { cue: boolean; recalled: { mid: string; kind: string; score: number }[] } }[];
};
export const getMemoryExplorer = (subject_type: string, subject_id: string) =>
  apiFetch<MemoryExplorerData>(`/demo/memory/explorer?subject_type=${encodeURIComponent(subject_type)}&subject_id=${encodeURIComponent(subject_id)}`);

export type MemoryProbe = {
  cue: boolean; full_consent: boolean; prompt_lines: string[];
  recalled: { mid: string; kind: string; when: string; text: string; score: number }[];
};
export const probeMemory = (subject_type: string, subject_id: string, q: string) =>
  apiFetch<MemoryProbe>(`/demo/memory/probe?subject_type=${encodeURIComponent(subject_type)}&subject_id=${encodeURIComponent(subject_id)}&q=${encodeURIComponent(q)}`);

export type TestCustomer = {
  key: string; name: string; tagline: string; blurb: string; kind: "invoice" | "mandate"; amount_inr: number;
  memory_label: string; seeded: boolean;
  tests: { say: string; expect: string }[];
  subject_type?: string; subject_id?: string; contact_id?: string;
  contact?: { can_call: boolean; block_reason: string | null; phone_verified: boolean; voice_consent: boolean; memory_consent?: boolean };
  tiers?: MemoryTiers; calls?: number;
};
export const listTestCustomers = () => apiFetch<TestCustomer[]>("/demo/personas");
export const seedTestCustomers = () => apiFetch<TestCustomer[]>("/demo/personas/seed", { method: "POST" });
export const resetTestCustomer = (key: string) =>
  apiFetch<TestCustomer>(`/demo/personas/${encodeURIComponent(key)}/reset`, { method: "POST" });

// ── Simulation Console: modes, one debt's queue, run-to-the-end buttons ──

export type DemoModes = {
  real_links: boolean;
  links_switch: boolean | null;
  debit: "real" | "success" | "insufficient_funds";
  test_mode: boolean;
  keys_present: boolean;
  window_days: number;
  links?: {
    mode: "real" | "stub"; test_mode: boolean; cap: number; real_links_created_here: number;
    stand_ins: number; limit_hits: number; account_links_total?: number | null;
  };
};

export type QueueItem = {
  id: string;
  kind: string;
  plain: string;
  title: string;
  detail: string;
  due_at: string;
  runs_at: string;
  runs_at_ist: string;
  days_ahead: number;
  customer_facing: boolean;
  uses_real_razorpay: boolean;
  played_result: boolean;
  may_carry_link: boolean;
  hint: string;
  warn: string | null;
};

export type DemoQueue = {
  subject: {
    subject_type: string; subject_id: string; title: string; status: string; settled: boolean;
    amount_inr: number; open_promise: { id: string; promised_date: string; amount_inr: number } | null;
  };
  items: QueueItem[];
  clock: DemoClock;
  modes: DemoModes;
  /** An authorised autopay whose debit failed: the failed-debit run that now carries the recovery. */
  follow_up: { subject_type: string; subject_id: string; title: string; status: string; settled: boolean; amount_inr: number } | null;
  /** For an autopay: what happens after the debit, by result (these steps cannot be queued before the result exists). */
  outlook: { when: string; tone: "ok" | "bad" | "info"; text: string }[];
};

export type DriveResult = {
  steps: { title: string; detail: string; tone: string; at: string }[];
  before: { title: string; status: string; settled: boolean; amount_inr: number };
  after: { title: string; status: string; settled: boolean; amount_inr: number };
  clock: DemoClock;
  stopped_at_cap: boolean;
  real_links_spent: number;
  real_debits_made: number;
};

export type CheckLinkResult = {
  checked: boolean; note?: string; razorpay_status?: string; applied?: string[]; closed?: string | null;
  payments_seen?: number; short_url?: string; is_stub?: boolean;
};

const q2 = (t: string, id: string) => `subject_type=${encodeURIComponent(t)}&subject_id=${encodeURIComponent(id)}`;
export const getModes = () => apiFetch<DemoModes>("/demo/modes");
export const setLinksMode = (real: boolean) => apiFetch<DemoModes>("/demo/links-mode", { method: "POST", body: { real } });
export const setDebitMode = (outcome: DemoModes["debit"]) => apiFetch<DemoModes>("/demo/debit-mode", { method: "POST", body: { outcome } });
export const getQueue = (t: string, id: string) => apiFetch<DemoQueue>(`/demo/queue?${q2(t, id)}`);
export const finishUnpaid = (t: string, id: string) =>
  apiFetch<DriveResult>("/demo/finish-unpaid", { method: "POST", body: { subject_type: t, subject_id: id } });
export const promiseBroken = (t: string, id: string) =>
  apiFetch<DriveResult>("/demo/promise-broken", { method: "POST", body: { subject_type: t, subject_id: id } });
export const checkLink = (t: string, id: string) =>
  apiFetch<CheckLinkResult>("/demo/check-link", { method: "POST", body: { subject_type: t, subject_id: id } });

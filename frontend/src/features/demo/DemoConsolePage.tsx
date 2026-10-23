import { useEffect, useRef, useState } from "react";
import { AuthNav, RegisterInvoiceButton } from "@/features/auth/AuthNav";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { MerchantKeyGate } from "@/features/dashboard/MerchantKeyGate";
import { RazorpayPanel } from "./RazorpayPanel";
import { ModesCard } from "./ModesCard";
import { QueuePanel } from "./QueuePanel";
import { ApiError } from "@/lib/api/client";
import {
  advanceClock,
  createInvoiceScenario,
  createMandateScenario,
  getDemoMessages,
  getDemoMoney,
  getDemoState,
  getDemoSubjects,
  getDemoTimeline,
  getRingingCalls,
  nextEvent,
  replyToCall,
  resetClock,
  setInteractive,
  simulatePayment,
  writeOffInvoice,
  getInvoiceLadder,
  listTestCustomers,
  resetTestCustomer,
  seedTestCustomers,
  getDemoMemory,
  forgetDemoMemory,
  promiseDayPaid,
  startCall,
  toWindow,
  type AdvanceResult,
  type DemoSubject,
  type RingingCall,
  type TestCustomer,
} from "@/lib/api/demo";

import { CallSimulator } from "./CallSimulator";
import { IncomingCall } from "./IncomingCall";
import { unlockAudio } from "./ringtone";
import { fmtIst, inr } from "./format";
import { MemoryView } from "./MemoryExplorer";
import { PhoneInbox } from "./PhoneInbox";
import { ControlBar } from "./ControlBar";

const POLL = 2500;
const SELECTED_KEY = "demo-console.selected";


const APPROACH_LABEL: Record<string, string> = {
  standard: "Standard: friendly and clear",
  gentle: "Gentle: slower, softer words, no hurry on the date",
  brief: "Brief: short and warm (kept their last promise)",
  steady: "Steady: calm, asks for one firm date",
  explain_first: "Explains the charge first",
};

function errText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: unknown } | null)?.detail;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    return e.message;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}

const BLOCK_LABEL: Record<string, string> = {
  phone_not_verified: "The number is not verified yet. The customer must enter the code on their page.",
  no_voice_consent: "The customer has not ticked consent for calls on their page.",
  no_phone_number: "No phone number on file.",
  outside_contact_window: "Outside the 08:00–19:00 IST contact window. Use “Jump to 08:00 IST”.",
  one_call_per_day: "A call was already placed for this debt today (a scheduled call may have used it). Move the clock forward a day, or use Reset this customer.",
  max_attempts_reached: "Call attempts for this debt are used up (3 per debt); it goes to a human. Use Reset this customer to start over.",
  promise_open: "The customer has an open promise, so calls are paused.",
  wrong_number_flagged: "This number was flagged as a wrong number.",
  run_not_active: "Chasing for this debt has already stopped (it is escalated to a human, closed or paid), so no call is placed.",
  retry_already_due: "The retry for this debt is already due, so a \"fund your account\" call would be pointless.",
  invoice_closed: "This invoice is closed.",
  invoice_disputed: "This invoice is disputed, so automated calls are stopped.",
  payment_resolved: "The payment is already resolved.",
  below_min_order_value: "Below the minimum order value for calls.",
  unknown_subject: "That debt no longer exists.",
  dnd_registered: "This number is on the Do-Not-Disturb list.",
};

const TONE_BG: Record<string, string> = {
  ok: "bg-soft/15 text-soft",
  info: "bg-ink/5 text-ink/60",
  warn: "bg-declined/20 text-declined",
  bad: "bg-failed/15 text-failed",
};
const TYPE_ICON: Record<string, string> = { message: "✉", call: "☎", promise: "🤝", job: "⚙" };

const PROMISE_HEAD: Record<string, string> = {
  waiting: "Waiting",
  due_today: "Due today",
  in_grace: "Date passed, not judged yet",
  kept: "KEPT",
  partly_kept: "Partly kept",
  broken: "BROKEN",
  cancelled: "Cancelled",
};
const PROMISE_CHIP: Record<string, string> = {
  waiting: "promise waiting",
  due_today: "promise due today",
  in_grace: "promise date passed",
  kept: "promise kept",
  partly_kept: "promise partly kept",
  broken: "promise broken",
  cancelled: "promise cancelled",
};
const PROMISE_BOX: Record<string, string> = {
  waiting: "bg-ink/5 border-ink/10",
  due_today: "bg-declined/15 border-declined/40",
  in_grace: "bg-declined/15 border-declined/40",
  kept: "bg-soft/10 border-soft/40 text-soft",
  partly_kept: "bg-declined/15 border-declined/40",
  broken: "bg-failed/10 border-failed/40 text-failed",
  cancelled: "bg-ink/5 border-ink/10",
};

const MONEY_BOX: Record<string, string> = {
  ok: "bg-soft/10 border-soft/40",
  info: "bg-ink/5 border-ink/10",
  warn: "bg-declined/15 border-declined/40",
  bad: "bg-failed/10 border-failed/40",
};

const STATUS_NOTE: Record<string, string> = {
  exhausted:
    "Automatic chasing has stopped and this debt is with a human (retries used up, a broken promise, or an unauthorised-debit claim). No more calls or retries will fire - create a new scenario to see the call.",
  halted: "The mandate is dead; a re-mandate link was sent. No retries will fire.",
  recovered: "Paid - nothing more will be chased.",
  disputed: "Disputed - automation stopped; a person decides.",
};

function Chip({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${ok ? "bg-soft/10 text-soft" : "bg-ink/5 text-ink/50"}`}>
      {ok ? "✓ " : "○ "}
      {children}
    </span>
  );
}

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="rounded-2xl bg-white border border-ink/5 p-5 shadow-[0_20px_40px_-34px_rgba(234,88,12,0.5)]">
      <div className="flex items-center justify-between gap-2 mb-3">
        <h2 className="font-display font-semibold">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

function Fold({ title, hint, children, always }: { title: string; hint?: string; children: React.ReactNode; always?: boolean }) {
  if (always) {      // a section that is part of the page, not hidden behind a click
    return (
      <section className="rounded-2xl bg-white border border-ink/10 shadow-sm">
        <div className="flex items-center gap-3 px-5 py-3.5">
          <span className="font-display font-semibold">{title}</span>
          {hint && <span className="text-xs text-ink/50 truncate">{hint}</span>}
        </div>
        <div className="px-5 pb-5 pt-1">{children}</div>
      </section>
    );
  }
  return (
    <details className="group rounded-2xl bg-white border border-ink/10 shadow-sm open:shadow-md">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-3.5 select-none">
        <span className="text-ink/40 transition-transform group-open:rotate-90">▶</span>
        <span className="font-display font-semibold">{title}</span>
        {hint && <span className="text-xs text-ink/50 truncate">{hint}</span>}
      </summary>
      <div className="px-5 pb-5 pt-1">{children}</div>
    </details>
  );
}

function ConsoleBody() {
  const qc = useQueryClient();
  // A link can name the customer (?customer=<debt id>); otherwise the last choice is restored.
  const params = new URLSearchParams(window.location.search);
  const [selected, setSelected] = useState<string | null>(() => {
    const fromLink = params.get("customer");
    if (fromLink) return fromLink;
    try { return localStorage.getItem(SELECTED_KEY); } catch { return null; }   // storage can be blocked: start unselected
  });
  const [note, setNote] = useState<string>("");
  const [view, setView] = useState<"console" | "memory">(
    new URLSearchParams(window.location.search).get("view") === "memory" ? "memory" : "console");   // "memory" replaces the rest of the page
  const [phone, setPhone] = useState("9876543210");
  const [language, setLanguage] = useState("hinglish");
  const [amount, setAmount] = useState(499);
  const [daysOverdue, setDaysOverdue] = useState(-5);
  const [kind, setKind] = useState<"mandate" | "invoice">("mandate");
  const [dismissedCall, setDismissedCall] = useState<string | null>(null);
  // The call being talked on. Held here (not read back from the "ringing" list) so the
  // popup stays open to show the outcome after the call has ended on the server.
  const [activeCall, setActiveCall] = useState<RingingCall | null>(null);

  // Several Demo Console tabs would all ring for the same call and compete for the microphone. The tab that accepts
  // a call tells the others, which then stop showing it.
  const callChannel = useRef<BroadcastChannel | null>(null);
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel("demo-console-calls");
    callChannel.current = ch;
    ch.onmessage = (e: MessageEvent) => {
      const id = (e.data as { claimed?: unknown } | null)?.claimed;
      if (typeof id === "string") setDismissedCall(id);
    };
    return () => { ch.close(); callChannel.current = null; };
  }, []);

  // Browsers keep sound off until the page has been clicked once.
  useEffect(() => {
    const unlock = () => unlockAudio();
    window.addEventListener("pointerdown", unlock, { once: true });
    return () => window.removeEventListener("pointerdown", unlock);
  }, []);

  const refetch = { refetchInterval: POLL };
  const stateQ = useQuery({ queryKey: ["demo", "state"], queryFn: getDemoState, ...refetch });
  const msgQ = useQuery({ queryKey: ["demo", "messages"], queryFn: getDemoMessages, ...refetch });
  const subjQ = useQuery({ queryKey: ["demo", "subjects"], queryFn: getDemoSubjects, ...refetch });
  const ringQ = useQuery({ queryKey: ["demo", "ringing"], queryFn: getRingingCalls, ...refetch });

  const personasQ = useQuery({ queryKey: ["demo", "test-customers"], queryFn: listTestCustomers, ...refetch });
  const personas: TestCustomer[] = personasQ.data ?? [];
  const personaBySubject = new Map(personas.filter((p) => p.subject_id).map((p) => [p.subject_id as string, p]));

  const [, setCreatedId] = useState<string | null>(null);
  const allSubjects = subjQ.data ?? [];
  const subjects = allSubjects;     // every person's debts: invoices, autopay debits, failed orders
  // The chosen customer, else the first test customer, else the newest debt.
  const current: DemoSubject | undefined =
    subjects.find((s) => s.subject_id === selected) ??
    subjects.find((s) => personaBySubject.has(s.subject_id)) ??
    subjects[0];
  const persona = current ? personaBySubject.get(current.subject_id) : undefined;
  const pick = (id: string) => {
    setSelected(id);
    setNote("");
    try { localStorage.setItem(SELECTED_KEY, id); } catch { /* the choice just is not remembered */ }
  };
  const timelineQ = useQuery({
    queryKey: ["demo", "timeline", current?.subject_id],
    queryFn: () => getDemoTimeline(current!.subject_type, current!.subject_id),
    enabled: !!current,
    refetchInterval: POLL,
  });

  const moneyQ = useQuery({
    queryKey: ["demo", "money", current?.subject_type, current?.subject_id],
    queryFn: () => getDemoMoney(current?.subject_type, current?.subject_id),
    refetchInterval: POLL,
  });
  const money = moneyQ.data;

  const refreshAll = () => qc.invalidateQueries({ queryKey: ["demo"] });
  const onAdvance = (r: AdvanceResult) => {
    const ran = r.ran;
    const bits = [
      r.moved_hours > 0 ? `Clock moved ${r.moved_hours.toFixed(1)} h` : "Clock unchanged",
      `${ran.executed} job${ran.executed === 1 ? "" : "s"} ran`,
    ];
    if (ran.deferred) bits.push(`${ran.deferred} deferred to the next contact window`);
    if (ran.skipped) bits.push(`${ran.skipped} skipped`);
    if (r.note) bits.push(r.note);
    setNote(bits.join(" · "));
    refreshAll();
  };
  const onErr = (e: unknown) => setNote(errText(e));

  const next = useMutation({
    mutationFn: (scoped: boolean) => nextEvent(scoped ? current?.subject_id : undefined),
    onSuccess: onAdvance,
    onError: onErr,
  });
  const plus = useMutation({ mutationFn: advanceClock, onSuccess: onAdvance, onError: onErr });
  const win = useMutation({ mutationFn: toWindow, onSuccess: onAdvance, onError: onErr });
  const reset = useMutation({
    mutationFn: resetClock,
    onSuccess: () => {
      setNote("Clock reset to real time.");
      refreshAll();
    },
  });
  const interactive = useMutation({ mutationFn: setInteractive, onSuccess: refreshAll, onError: onErr });

  // The customer's own page opens in a new tab as soon as a scenario is created, so you can verify the number and give
  // (or withhold) consent as the customer. The tab is opened inside the click (browsers block popups opened later, from a
  // response) and pointed at the page once the debt exists.
  const customerTab = useRef<Window | null>(null);
  const startCreate = () => {
    try { customerTab.current = window.open("about:blank", "_blank"); } catch { customerTab.current = null; }
    create.mutate();
  };
  const create = useMutation({
    mutationFn: async () => {
      if (kind === "mandate") {
        return createMandateScenario({ phone, language, amount_inr: amount, reason: "insufficient_funds" });
      }
      return createInvoiceScenario({ phone, language, amount_inr: amount, days_overdue: daysOverdue });
    },
    onSuccess: async (r) => {
      if (r.subject_id) { setCreatedId(r.subject_id); pick(r.subject_id); }
      const tab = customerTab.current;
      customerTab.current = null;
      if (tab && r.self_serve_path) {
        tab.location.href = `${window.location.origin}${r.self_serve_path}`;
        setNote("Customer created. Their page opened in a new tab: verify the number there, then give or withhold call consent.");
      } else {
        setNote("Customer created. Open “Customer's page ↗” at the top to verify the number and give or withhold consent.");
      }
      await refreshAll();
    },
    onError: (e) => {
      customerTab.current?.close();          // do not leave an empty tab behind
      customerTab.current = null;
      onErr(e);
    },
  });

  const pay = useMutation({
    mutationFn: () => simulatePayment(current!.subject_type, current!.subject_id),
    onSuccess: () => {
      setNote("Payment simulated through the same handler a Razorpay webhook reaches.");
      refreshAll();
    },
    onError: onErr,
  });

  const ladderQ = useQuery({
    queryKey: ["demo-ladder", current?.subject_id],
    queryFn: () => getInvoiceLadder(current!.subject_id),
    enabled: !!current && current.subject_type === "invoice",
    refetchInterval: 3000,
  });
  const memQ = useQuery({
    queryKey: ["demo", "memory", current?.subject_id],
    queryFn: () => getDemoMemory(current!.subject_type, current!.subject_id),
    enabled: !!current,
    refetchInterval: 5000,
  });
  const forget = useMutation({
    mutationFn: () => forgetDemoMemory(current!.subject_type, current!.subject_id),
    onSuccess: (r) => {
      setNote(`Forgot ${r.deleted} item(s) kept about this customer.`);
      void memQ.refetch();
      void qc.invalidateQueries({ queryKey: ["demo", "memory-explorer"] });
    },
    onError: onErr,
  });

  const writeOff = useMutation({
    mutationFn: () => writeOffInvoice(current!.subject_id, "Demo: finance decided this will not be collected"),
    onSuccess: () => {
      setNote("Finance wrote the invoice off. The money moved from “Receivables overdue” to “Receivables written off”.");
      refreshAll();
      ladderQ.refetch();
    },
    onError: onErr,
  });

  const keep = useMutation({
    mutationFn: () => promiseDayPaid(current!.subject_type, current!.subject_id),
    onSuccess: () => {
      setNote("The promised day arrived, the bank retry went through and the payment landed. The promise should now read “Kept”.");
      refreshAll();
    },
    onError: onErr,
  });

  const place = useMutation({
    mutationFn: () => startCall(current!.subject_type, current!.subject_id),
    onSuccess: (r) => {
      if (!r.ok) setNote(BLOCK_LABEL[r.block] ?? `Call blocked: ${r.block}`);
      else setNote("Ringing - accept the call in the pop-up to talk to the agent.");
      refreshAll();
    },
    onError: onErr,
  });

  const seedP = useMutation({
    mutationFn: seedTestCustomers,
    onSuccess: (list) => {
      setNote("Test customers are ready.");
      const first = list.find((p) => p.subject_id);
      if (first?.subject_id && !selected) pick(first.subject_id);
      refreshAll();
    },
    onError: onErr,
  });
  const resetP = useMutation({
    mutationFn: (key: string) => resetTestCustomer(key),
    onSuccess: (p) => {
      setNote(`${p.name} is back to the starting state.`);
      if (p.subject_id) setSelected(p.subject_id);
      refreshAll();
    },
    onError: onErr,
  });

  const state = stateQ.data;

  // This console exists to talk to the agent, so scheduled calls must ring HERE. With the setting off, a
  // "Next event" places the call instantly with a canned customer and nothing rings. The setting lives in the
  // server's memory, so a server restart resets it: switch it back on whenever the server reports it off, unless
  // the person switched it off themselves with the checkbox in the clock card.
  const ringOffByUser = useRef(false);
  const ringNoticed = useRef(false);
  useEffect(() => {
    if (state && !state.interactive_calls && !ringOffByUser.current) {
      interactive.mutate(true);
      if (!ringNoticed.current) {
        ringNoticed.current = true;
        setNote("Scheduled calls now ring in this browser, so you can answer them as the customer.");
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.interactive_calls]);
  const messages = msgQ.data ?? [];
  const mine = messages.filter((m) => m.subject_id === current?.subject_id);
  const timeline = timelineQ.data ?? [];
  // Only the SELECTED customer's call rings on screen. The clock is shared, so moving it runs every debt's jobs and
  // several calls can ring at once; those for other customers wait here instead of popping up one after another.
  const waitingCalls = (ringQ.data ?? []).filter((c) => c.call_id !== dismissedCall && c.call_id !== activeCall?.call_id);
  const ringing = waitingCalls.find((c) => c.subject_id === current?.subject_id);
  const elsewhere = waitingCalls.filter((c) => c.subject_id !== current?.subject_id);
  const switchTo = elsewhere.find((c) => personaBySubject.has(c.subject_id));
  const declineElsewhere = async () => {
    try {
      await Promise.all(elsewhere.map((c) => replyToCall(c.call_id, "")));
      setNote(`${elsewhere.length} call(s) for other customers were declined (recorded as no answer).`);
    } catch (e) {
      setNote(errText(e));
    }
    void refreshAll();
  };

  const c = current?.contact;
  const latestPromise = current?.promises?.[0];
  const noticeSent = mine.some((m) => m.template !== "otp" && m.status !== "not_sent_no_contact");
  const hadCall = timeline.some((t) => t.type === "call");
  const steps: { done: boolean; text: string }[] = current
    ? [
        { done: true, text: "Debt created and the recovery plan queued" },
        { done: !!c?.phone_verified, text: "Customer opens their page and verifies the number with the code from the inbox" },
        { done: !!c?.voice_consent, text: "Customer ticks consent for calls (only they can)" },
        { done: noticeSent, text: "A notice goes out — press “Next event ▶” at the top to move to it" },
        { done: hadCall, text: "The recovery call rings — answer it as the customer (tip: do NOT pick a pay-by date on the customer page first; a promise pauses the call)" },
        { done: !!current.open_promise || current.settled, text: "A promise to pay is recorded and chasing pauses" },
        { done: current.settled, text: "Payment arrives and the ledger closes the debt exactly once" },
      ]
    : [];

  const ledger = money?.ledger;
  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <header className="border-b border-ink/10 bg-cream/80 sticky top-0 z-20 backdrop-blur">
        <div className="mx-auto max-w-[1320px] px-6 h-16 flex items-center gap-6">
          <Link to="/" className="font-display font-semibold text-lg tracking-tight">Backstop</Link>
          <nav className="flex items-center gap-1 text-sm">
            <Link to="/dashboard" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">Dashboard</Link>
            <Link to="/store" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">Storefront</Link>
            <Link to="/architecture" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">Architecture</Link>
            <Link to="/tiers" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">Tiers</Link>
            <span className="px-3 py-1.5 rounded-md text-ink font-semibold">Simulation Console</span>
          </nav>
          <div className="ml-auto flex items-center gap-3"><RegisterInvoiceButton /><AuthNav /></div>
        </div>
      </header>

      <main className="mx-auto max-w-[1320px] px-6 py-6 space-y-5">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">Simulation Console</h1>
          <p className="text-sm text-ink/60 mt-0.5 max-w-[860px]">
            Pick a debt, then press <strong>Next event ▶</strong> to walk its queue. The rules, scheduler, guards, ledger and audit log are
            the real ones; the SMS carrier, the phone line and the clock are simulated.
          </p>
        </div>

        <ControlBar
          subjects={subjects}
          personas={personas}
          current={current}
          persona={persona}
          state={state}
          onSelect={pick}
          onSeed={() => seedP.mutate()} seeding={seedP.isPending}
          onResetCustomer={() => persona && resetP.mutate(persona.key)} resetting={resetP.isPending}
          onNext={() => next.mutate(true)} nexting={next.isPending}
          onPlaceCall={() => place.mutate()} calling={place.isPending}
          onPay={() => pay.mutate()} paying={pay.isPending}
          onPlus={(h) => plus.mutate(h)} onWindow={() => win.mutate()} onNextAny={() => next.mutate(false)}
          onResetClock={() => reset.mutate()} onInteractive={(on) => { ringOffByUser.current = !on; interactive.mutate(on); }}
          busyClock={plus.isPending || win.isPending || next.isPending}
        />

        {note && (
          <p className="text-sm rounded-xl bg-white border border-ember/30 border-l-4 border-l-ember px-4 py-2.5 text-ink/80 shadow-sm">{note}</p>
        )}

        {elsewhere.length > 0 && (
          <div className="rounded-xl border border-ember/40 bg-ember/5 px-4 py-2.5 text-sm flex flex-wrap items-center gap-3">
            <span className="flex-1 min-w-[240px]">
              📞 {elsewhere.length} call{elsewhere.length === 1 ? " is" : "s are"} ringing for{" "}
              {elsewhere.length === 1 ? "another customer" : "other customers"}
              {switchTo ? ` (${personaBySubject.get(switchTo.subject_id)?.name})` : ""}. It will not pop up while you work on this one.
            </span>
            {switchTo && (
              <button type="button" onClick={() => pick(switchTo.subject_id)}
                className="px-3 py-1.5 rounded-md bg-ink text-cream text-xs font-semibold">Switch to them</button>
            )}
            <button type="button" onClick={() => void declineElsewhere()}
              className="px-3 py-1.5 rounded-md border border-ink/20 text-xs">Decline {elsewhere.length === 1 ? "it" : "them"}</button>
          </div>
        )}

        {view === "memory" && current ? (
          <MemoryView
            subject={current}
            name={persona?.name ?? current.title}
            onForget={() => forget.mutate()}
            forgetting={forget.isPending}
            onBack={() => setView("console")}
          />
        ) : (
          <>
            {/* money, in one thin strip: the same audited ledger the dashboard shows */}
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
              {(
                [
                  ["At risk · autopay", ledger?.at_risk_mandate, "text-declined"],
                  ["Recovered", ledger?.total_recovered, "text-soft"],
                  ["Lost", ledger?.total_lost, "text-failed"],
                  ["Invoices overdue", ledger?.receivables.overdue, "text-declined"],
                  ["Invoices collected", ledger?.receivables.collected, "text-soft"],
                  ["Written off", ledger?.receivables.written_off, "text-failed"],
                ] as [string, number | undefined, string][]
              ).map(([label, v, cls]) => (
                <div key={label} className="rounded-xl bg-white border border-ink/5 px-3.5 py-2.5 shadow-sm">
                  <div className="text-[11px] text-ink/50 leading-tight">{label}</div>
                  <div className={`font-display text-lg font-semibold ${cls}`}>{v === undefined ? "…" : inr(v)}</div>
                </div>
              ))}
            </div>

            {!current ? (
              <Card title="No debt selected">
                <p className="text-sm text-ink/60">Use “Load test customers” above, or create a debt under “Create a debt to simulate” below.</p>
              </Card>
            ) : (
              <div className="grid lg:grid-cols-[1.25fr_1fr] gap-5 items-start">
                <div className="space-y-5">
                  <QueuePanel subject={current} onNote={setNote} onOpen={pick} />

                  <Card title="Where things stand">
                    <div className="space-y-3">
                      {STATUS_NOTE[current.status] && (
                        <p className="text-xs rounded-md bg-declined/10 text-declined px-2.5 py-1.5">{STATUS_NOTE[current.status]}</p>
                      )}
                      {current.subject_type === "mandate" && (
                        <div className="rounded-xl border border-soft/30 bg-soft/10 px-3.5 py-3 text-sm space-y-1.5">
                          <span className="text-[11px] font-semibold uppercase tracking-wide text-soft">An authorised card autopay</span>
                          <p>Nothing is owed yet. <strong>Next event</strong> runs its pre-debit notice, then the debit.</p>
                          <p className="text-ink/70">
                            What the debit does follows “Razorpay &amp; simulation settings” below (now:{" "}
                            <strong>{state?.modes?.debit === "real" || !state?.modes ? "ask Razorpay for real" : `played result, ${state.modes.debit.replace("_", " ")}`}</strong>).
                            Choose “Play: debit fails” there to see the recovery: the failed debit then appears as its own item and this page offers to open it.
                          </p>
                        </div>
                      )}
                      {current.subject_type !== "mandate" && money?.subject == null && !latestPromise && !STATUS_NOTE[current.status] && (
                        <p className="text-sm text-ink/50">Nothing has happened to this debt yet.</p>
                      )}
                      {money?.subject && (
                        <div className={`rounded-xl border px-3.5 py-3 ${MONEY_BOX[money.subject.tone]}`}>
                          <span className="text-[11px] font-semibold uppercase tracking-wide opacity-70">Where this money is</span>
                          <p className="text-sm font-medium mt-1">{money.subject.headline}</p>
                          <p className="text-sm mt-1 opacity-80">{money.subject.detail}</p>
                          {money.subject.state === "at_risk" && money.subject.hours_to_lapse !== null && money.subject.hours_to_lapse > 0 && (
                            <button type="button" onClick={() => plus.mutate(Math.ceil(money.subject!.hours_to_lapse!) + 1)} disabled={plus.isPending}
                              className="mt-2 text-xs px-3 py-1.5 rounded-md border border-ink/20 bg-white hover:bg-cream">
                              Fast-forward to that deadline ({Math.ceil(money.subject.hours_to_lapse / 24)} days)
                            </button>
                          )}
                          {money.trail.length > 0 && (
                            <ul className="mt-2 space-y-1 text-xs">
                              {money.trail.map((t, i) => (
                                <li key={i} className="flex gap-2">
                                  <span className="text-ink/40 w-24 shrink-0">{fmtIst(t.at)}</span>
                                  <span>
                                    <strong>{t.field_label}</strong>: {inr(t.before)} → {inr(t.after)}{" "}
                                    <span className="opacity-60">({t.delta >= 0 ? "+" : "−"}{inr(Math.abs(t.delta))}, {t.why})</span>
                                  </span>
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      )}

                      {latestPromise && (
                        <div className={`rounded-xl border px-3.5 py-3 ${PROMISE_BOX[latestPromise.state]}`}>
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-[11px] font-semibold uppercase tracking-wide">Promise to pay · {PROMISE_HEAD[latestPromise.state]}</span>
                            <span className="text-xs">
                              {inr(latestPromise.amount_inr)} by{" "}
                              {new Date(latestPromise.promised_date).toLocaleDateString("en-IN", { day: "2-digit", month: "short" })}
                            </span>
                          </div>
                          <p className="text-sm mt-1">{latestPromise.label}</p>
                          {current.open_promise && !current.settled && (
                            <button type="button" onClick={() => keep.mutate()} disabled={keep.isPending}
                              className="mt-2 px-3 py-1.5 rounded-md bg-ember text-white text-xs font-semibold disabled:opacity-40">
                              Promised day arrives → customer pays
                            </button>
                          )}
                          {current.promises.length > 1 && (
                            <p className="text-xs mt-2 opacity-70">
                              Earlier promises:{" "}
                              {current.promises.slice(1).map((p) => `${p.promised_date} (${(PROMISE_HEAD[p.state] ?? p.state).toLowerCase()})`).join(", ")}
                            </p>
                          )}
                        </div>
                      )}

                      <div className="flex flex-wrap items-center gap-1.5">
                        <Chip ok={!!c?.has_phone}>number {c?.phone_masked || "missing"}</Chip>
                        <Chip ok={!!c?.phone_verified}>verified</Chip>
                        <Chip ok={!!c?.voice_consent}>call consent</Chip>
                        <Chip ok={latestPromise?.state === "kept"}>{latestPromise ? PROMISE_CHIP[latestPromise.state] : "no promise"}</Chip>
                        <Chip ok={current.settled}>settled</Chip>
                        {current.subject_type === "invoice" && (
                          <button type="button"
                            onClick={() => { if (window.confirm("Write this invoice off as uncollectable? This is a finance decision.")) writeOff.mutate(); }}
                            disabled={writeOff.isPending || current.settled}
                            className="ml-auto px-3 py-1.5 rounded-md border border-ember text-ember text-xs font-semibold hover:bg-ember/5 disabled:opacity-40">
                            Finance writes this off
                          </button>
                        )}
                      </div>
                    </div>
                  </Card>

                  {current.subject_type === "invoice" && (
                    <Card title="How this invoice is chased">
                      <ol className="space-y-2.5">
                        {(ladderQ.data?.steps ?? []).map((st) => (
                          <li key={st.step} className="text-sm">
                            <div className="flex items-baseline gap-2">
                              <span className={`size-2 rounded-full shrink-0 translate-y-[-1px] ${
                                st.status === "done" ? "bg-soft" : st.status === "scheduled" ? "bg-ink/30" : st.status === "cancelled" ? "bg-ink/15" : "bg-ember"}`} />
                              <span className="font-medium">{st.label}</span>
                              <span className="text-xs text-ink/50">{st.due} IST</span>
                            </div>
                            <p className="ml-4 text-xs text-ink/70">{st.status === "scheduled" ? "Waiting." : `${st.title}. ${st.detail}`}</p>
                            {st.hint && <p className="ml-4 text-xs text-ember">{st.hint}</p>}
                          </li>
                        ))}
                      </ol>
                      <p className="text-xs text-ink/50 mt-3">Grey = waiting · green = done · orange = skipped or changed.</p>
                    </Card>
                  )}

                  <Card title="What happened to this debt">
                    {timeline.length === 0 ? (
                      <p className="text-sm text-ink/50">Nothing yet.</p>
                    ) : (
                      <ul className="space-y-3 text-sm">
                        {timeline.map((t, i) => (
                          <li key={i} className="flex gap-3" title={t.raw}>
                            <span className={`mt-0.5 size-6 shrink-0 rounded-full grid place-items-center text-xs ${TONE_BG[t.tone]}`}>{TYPE_ICON[t.type]}</span>
                            <span className="min-w-0">
                              <span className="block font-medium">{t.title}</span>
                              {t.detail && <span className="block text-ink/60">{t.detail}</span>}
                              {t.body && (
                                <span className="block mt-0.5 text-xs text-ink/50 italic break-words">
                                  {t.type === "call" ? `They said: “${t.body}”` : t.body}
                                </span>
                              )}
                              <span className="block text-[11px] text-ink/40 mt-0.5">{fmtIst(t.at)} IST</span>
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </Card>
                </div>

                <div className="space-y-5 lg:sticky lg:top-20">
                  <Card title="Customer's phone" right={<span className="text-xs text-ink/40">{mine.length} message(s)</span>}>
                    <PhoneInbox messages={mine} />
                  </Card>

                  {memQ.data?.approach && (
                    <Card title="How the agent will approach this customer">
                      <p className="text-sm text-ink">
                        <span className="font-semibold">{APPROACH_LABEL[memQ.data.approach.name]}</span>
                        {memQ.data.approach.pay_day ? ` · usual pay day: the ${memQ.data.approach.pay_day}th` : ""}
                        {memQ.data.approach.call_slot ? ` · likes ${memQ.data.approach.call_slot} calls` : ""}
                      </p>
                      <ul className="mt-2 space-y-1 text-xs text-ink/60 list-disc pl-4">
                        {memQ.data.approach.why.map((w) => <li key={w}>{w}</li>)}
                      </ul>
                      <p className="mt-2 text-xs text-ink/45">
                        {memQ.data.approach.facts_used
                          ? "Worked out from our records and what the customer let us remember. None of it is read out, apart from offering their usual pay day as a date to confirm."
                          : "Worked out from our own records only: the customer has not agreed to memory, so nothing they told us is used."}
                      </p>
                    </Card>
                  )}

                  <Card title={`What the agent remembers about ${persona?.name ?? "this customer"}`}>
                    <div className="flex flex-wrap items-center gap-3">
                      <p className="text-sm text-ink/70 flex-1 min-w-[200px]">
                        {memQ.data?.memory_consent_current
                          ? `${memQ.data.tiers?.episodes ?? 0} call(s) remembered · ${memQ.data.tiers?.facts ?? 0} fact(s) · ${memQ.data.tiers?.chunks ?? 0} word-for-word window(s).`
                          : memQ.data?.memory_consent
                            ? "Only older short notes are kept (the customer has not agreed to the current wording)."
                            : "Nothing is kept: the customer has not agreed to memory."}
                      </p>
                      {current.subject_type !== "mandate" && (
                        <button type="button" onClick={() => setView("memory")}
                          className="px-4 py-2 rounded-lg bg-ink text-cream text-sm font-semibold hover:bg-ink/90">Open memory view →</button>
                      )}
                    </div>
                  </Card>
                </div>
              </div>
            )}

            {/* the rarely-needed things live below, folded away */}
            <Fold always title="Razorpay &amp; simulation settings"
              hint={`links: ${state?.modes?.real_links ? "real" : "stand-ins"} · autopay debits: ${state?.modes?.debit === "real" || !state?.modes ? "ask Razorpay" : `played (${state.modes.debit.replace("_", " ")})`}`}>
              <ModesCard />
            </Fold>

            <Fold always title="Create a debt to simulate" hint="a failed autopay debit or an overdue invoice for a number you choose">
              <div className="flex gap-2 mb-3">
                {(["mandate", "invoice"] as const).map((k) => (
                  <button key={k} type="button" onClick={() => { setKind(k); setAmount(k === "mandate" ? 499 : 3000); }}
                    className={`px-3 py-1.5 rounded-md text-sm border ${kind === k ? "bg-ink text-cream border-ink" : "border-ink/15"}`}>
                    {k === "mandate" ? "Failed autopay debit (consumer)" : "Overdue invoice (business)"}
                  </button>
                ))}
              </div>
              <div className="grid sm:grid-cols-3 gap-3">
                <label className="text-xs text-ink/60">Customer phone
                  <input value={phone} onChange={(e) => setPhone(e.target.value)}
                    className="mt-1 w-full rounded-md border border-ink/15 px-2 py-1.5 text-sm text-ink" />
                </label>
                <label className="text-xs text-ink/60">Language
                  <select value={language} onChange={(e) => setLanguage(e.target.value)}
                    className="mt-1 w-full rounded-md border border-ink/15 px-2 py-1.5 text-sm text-ink bg-white">
                    <option value="hinglish">Hinglish</option>
                    <option value="hi">हिन्दी</option>
                    <option value="en">English</option>
                  </select>
                </label>
                <label className="text-xs text-ink/60">Amount (₹)
                  <input type="number" value={amount} onChange={(e) => setAmount(Number(e.target.value))}
                    className="mt-1 w-full rounded-md border border-ink/15 px-2 py-1.5 text-sm text-ink" />
                </label>
                {kind === "invoice" && (
                  <label className="text-xs text-ink/60 sm:col-span-3">Where is the invoice in its life?
                    <select value={daysOverdue} onChange={(e) => setDaysOverdue(Number(e.target.value))}
                      className="mt-1 w-full rounded-md border border-ink/15 px-2 py-1.5 text-sm text-ink bg-white">
                      <option value={-5}>Not due yet — due in 5 days (the whole ladder will run)</option>
                      <option value={8}>8 days overdue (courtesy + first link already missed)</option>
                      <option value={20}>20 days overdue (only the last steps remain)</option>
                    </select>
                  </label>
                )}
              </div>
              <button type="button" onClick={startCreate} disabled={create.isPending}
                className="mt-3 px-4 py-2 rounded-lg bg-ember text-white text-sm font-semibold disabled:opacity-50">Create and select it</button>
              <p className="text-xs text-ink/50 mt-2">
                The number starts unverified and without consent. The customer's page opens in a new tab, where they verify and decide, as in real use.
                For a real invoice with a real business login, use <Link to="/invoices" className="underline">Register invoice</Link>.
              </p>
            </Fold>

            <Fold always title="Real Razorpay mandates (Test Mode)" hint="authorised cards, queue a debit, check Razorpay, cancel">
              <RazorpayPanel />
            </Fold>

            <Fold title="Guided walkthrough" hint="the steps of one full recovery, ticked as they happen">
              <ol className="space-y-1.5">
                {steps.map((s, i) => (
                  <li key={i} className={`flex gap-2 text-sm ${s.done ? "text-ink" : "text-ink/50"}`}>
                    <span className={`mt-0.5 size-4 shrink-0 rounded-full text-[10px] grid place-items-center ${s.done ? "bg-soft text-white" : "bg-ink/10"}`}>{s.done ? "✓" : i + 1}</span>
                    {s.text}
                  </li>
                ))}
              </ol>
            </Fold>
          </>
        )}
      </main>

      {ringing && !activeCall && (
        <IncomingCall
          key={`ring-${ringing.call_id}`}
          call={ringing}
          onAccept={() => { callChannel.current?.postMessage({ claimed: ringing.call_id }); setActiveCall(ringing); }}
          onResolved={(r, how) => {
            setDismissedCall(ringing.call_id);
            setNote(
              (how === "missed" ? "The call rang out and was missed. " : "You declined the call. ") +
                (r?.routed_to?.startsWith("callback_scheduled")
                  ? "No answer was recorded, so the system planned another try for tomorrow morning."
                  : (r?.explanation ?? "")),
            );
            refreshAll();
          }}
        />
      )}
      {activeCall && (
        <CallSimulator
          key={activeCall.call_id}
          call={activeCall}
          onDone={() => {
            setDismissedCall(activeCall.call_id);
            setActiveCall(null);
            refreshAll();
          }}
        />
      )}
    </div>
  );
}

export function DemoConsolePage() {
  return (
    <MerchantKeyGate>
      <ConsoleBody />
    </MerchantKeyGate>
  );
}

import { useState } from "react";
import { ChevronDown } from "lucide-react";

import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { DemoState, DemoSubject, TestCustomer } from "@/lib/api/demo";

import { inr } from "./format";

const BLOCK_TEXT: Record<string, string> = {
  phone_not_verified: "number not verified", no_voice_consent: "no call consent",
  no_phone_number: "no number", wrong_number_flagged: "wrong number flagged",
};
const TYPE_LABEL: Record<string, string> = { mandate: "Autopay", mandate_run: "Failed autopay", invoice: "Invoice", order: "Failed order" };
const TYPE_DOT: Record<string, string> = { mandate: "bg-soft", mandate_run: "bg-loyal", invoice: "bg-ember", order: "bg-declined" };
const FILTERS: { key: string; label: string }[] = [
  { key: "all", label: "All" }, { key: "invoice", label: "Invoices" }, { key: "autopay", label: "Autopay" },
  { key: "order", label: "Orders" }, { key: "promise", label: "Promises" },
];

function Pill({ tone, children }: { tone: "ok" | "warn" | "plain"; children: React.ReactNode }) {
  const cls = tone === "ok" ? "bg-soft/15 text-soft" : tone === "warn" ? "bg-ember/10 text-ember" : "bg-ink/5 text-ink/60";
  return <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap ${cls}`}>{children}</span>;
}

/** The first section of the console: pick a debt, see the clock, and do the few things you do to a debt - all in one
 *  place. Everything else on the page follows the debt chosen here. */
export function ControlBar({
  subjects, personas, current, persona, state,
  onSelect, onSeed, seeding, onResetCustomer, resetting,
  onNext, nexting, onPlaceCall, calling, onPay, paying,
  onPlus, onWindow, onNextAny, onResetClock, onInteractive, busyClock,
}: {
  subjects: DemoSubject[];
  personas: TestCustomer[];
  current: DemoSubject | undefined;
  persona: TestCustomer | undefined;
  state: DemoState | undefined;
  onSelect: (subjectId: string) => void;
  onSeed: () => void; seeding: boolean;
  onResetCustomer: () => void; resetting: boolean;
  onNext: () => void; nexting: boolean;
  onPlaceCall: () => void; calling: boolean;
  onPay: () => void; paying: boolean;
  onPlus: (hours: number) => void; onWindow: () => void; onNextAny: () => void; onResetClock: () => void;
  onInteractive: (on: boolean) => void; busyClock: boolean;
}) {
  const [filter, setFilter] = useState("all");
  const seeded = personas.filter((p) => p.seeded && p.subject_id);
  const personaSubjects = new Set(seeded.map((p) => p.subject_id));
  const keep = (s: DemoSubject) =>
    filter === "all" ? true : filter === "promise" ? !!s.open_promise || (s.promises?.length ?? 0) > 0
      : filter === "autopay" ? s.subject_type === "mandate" || s.subject_type === "mandate_run" : s.subject_type === filter;
  const shown = subjects.filter(keep);
  const others = shown.filter((s) => !personaSubjects.has(s.subject_id));
  const byType = (t: string) => others.filter((s) => s.subject_type === t);
  const label = (s: DemoSubject) =>
    `${s.title.replace(/^(Invoice|Autopay|Failed autopay debit|Failed order) · /, "")} · ${inr(s.amount_inr)} · ${s.settled ? "closed" : s.status.replace(/_/g, " ")}${s.open_promise ? " · promise" : ""}`;
  const hasUnseeded = personas.some((p) => !p.seeded);
  const c = current?.contact;
  const clock = state?.clock;

  return (
    <section className="rounded-2xl bg-white border border-ink/10 shadow-[0_18px_40px_-28px_rgba(43,29,18,0.45)] overflow-hidden">
      {/* ── clock strip ── */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-5 py-3 bg-gradient-to-r from-ink to-[#4a2f1a] text-cream">
        <div className="flex items-baseline gap-3">
          <span className="font-display text-2xl font-semibold tracking-tight">{clock?.now_ist ?? "…"}</span>
          <span className="text-xs text-cream/60">IST{clock?.shifted ? ` · +${clock.offset_hours} h ahead of real time` : " · real time"}</span>
        </div>
        <span className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full ${clock?.in_contact_window ? "bg-soft/25 text-[#bfe9d6]" : "bg-cream/10 text-cream/70"}`}>
          <span className={`size-1.5 rounded-full ${clock?.in_contact_window ? "bg-[#7fd3b0]" : "bg-cream/40"}`} />
          {clock?.in_contact_window ? "Inside calling hours" : "Outside 08:00–19:00 IST"}
        </span>
        <span className="text-xs text-cream/60">{state?.scheduled_pending ?? 0} job(s) queued in total · the clock is shared by every debt</span>
        <div className="ml-auto flex items-center gap-2 text-xs">
          {state && (
            <span className="hidden lg:flex items-center gap-1.5 text-cream/50">
              SMS {state.providers.messaging} · OTP {state.providers.otp} · Voice {state.providers.voice}
            </span>
          )}
        </div>
      </div>

      {/* ── debt picker ── */}
      <div className="px-5 pt-4 pb-3 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm font-semibold">
            Debt
            <select value={current?.subject_id ?? ""} onChange={(e) => onSelect(e.target.value)}
              className="min-w-[320px] max-w-[540px] rounded-lg border border-ink/20 px-3 py-2 text-sm font-normal bg-white focus:outline-none focus:ring-2 focus:ring-ember/40">
              {seeded.length > 0 && filter === "all" && (
                <optgroup label="Test customers (with history)">
                  {seeded.map((p) => <option key={p.key} value={p.subject_id ?? ""}>{p.name} · {p.tagline}</option>)}
                </optgroup>
              )}
              {(["mandate", "invoice", "mandate_run", "order"] as const).map((t) => byType(t).length > 0 && (
                <optgroup key={t} label={t === "mandate" ? "Autopays (authorised cards)" : t === "invoice" ? "Invoices" : t === "mandate_run" ? "Failed autopay debits" : "Failed orders"}>
                  {byType(t).map((s) => <option key={s.subject_id} value={s.subject_id}>{label(s)}</option>)}
                </optgroup>
              ))}
              {current && !shown.some((s) => s.subject_id === current.subject_id) && !personaSubjects.has(current.subject_id) && (
                <option value={current.subject_id}>{label(current)}</option>
              )}
              {subjects.length === 0 && <option value="">No debts yet</option>}
            </select>
          </label>
          <span className="flex flex-wrap gap-1">
            {FILTERS.map((f) => (
              <button key={f.key} type="button" onClick={() => setFilter(f.key)}
                className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${filter === f.key ? "bg-ink text-cream border-ink" : "border-ink/15 text-ink/60 hover:border-ink/40"}`}>
                {f.label}
              </button>
            ))}
          </span>
          {hasUnseeded && (
            <button type="button" onClick={onSeed} disabled={seeding}
              className="ml-auto px-3 py-1.5 rounded-lg bg-ember/10 text-ember text-xs font-semibold hover:bg-ember/20 disabled:opacity-50">
              {seeding ? "Creating…" : seeded.length ? "Add missing test customers" : "Load test customers"}
            </button>
          )}
        </div>

        {/* ── who it is ── */}
        {current ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl bg-cream/70 border border-ink/5 px-4 py-3">
            <span className={`size-2.5 rounded-full ${TYPE_DOT[current.subject_type] ?? "bg-ink/30"}`} />
            <div className="min-w-[200px]">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-display font-semibold text-lg leading-tight">{persona?.name ?? current.title.replace(/^(Invoice|Autopay|Failed autopay debit|Failed order) · /, "")}</span>
                <span className="text-sm text-ink/60">{TYPE_LABEL[current.subject_type] ?? current.subject_type} · {inr(current.amount_inr)}</span>
              </div>
              {persona && <p className="text-xs text-ink/60 mt-0.5 max-w-[640px]">{persona.blurb}</p>}
            </div>
            <div className="flex flex-wrap gap-1.5 ml-auto">
              <Pill tone={c?.can_call ? "ok" : "warn"}>
                {c?.can_call ? "can be called" : `cannot be called: ${BLOCK_TEXT[c?.block_reason ?? ""] ?? c?.block_reason ?? "no contact"}`}
              </Pill>
              <Pill tone="plain">{persona?.memory_label ?? (c?.memory_consent ? "memory consent" : "no memory consent")}</Pill>
              {current.open_promise && <Pill tone="plain">promise open</Pill>}
              {current.settled && <Pill tone="ok">{current.status === "recovered" ? "paid" : "closed"}</Pill>}
            </div>
          </div>
        ) : (
          <p className="text-sm text-ink/60 rounded-xl bg-sand/50 px-4 py-3">Load the test customers, or create a debt under “Create a debt to simulate” below, to begin.</p>
        )}

        {/* ── actions ── */}
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={onNext} disabled={nexting || !current}
            title="Move the clock to this debt's next queued step and run it"
            className="px-6 py-2.5 rounded-lg bg-ember text-white text-sm font-semibold shadow-sm hover:bg-ember/90 disabled:opacity-40 transition-colors">
            {nexting ? "Running…" : "Next event ▶"}
          </button>
          <button type="button" onClick={onPlaceCall} disabled={calling || !current || current.settled || current.subject_type === "mandate"}
            title={current?.subject_type === "mandate" ? "Nothing is owed yet: calls start after a debit fails" : "Call the customer about this debt"}
            className="px-4 py-2.5 rounded-lg bg-ink text-cream text-sm font-semibold hover:bg-ink/90 disabled:opacity-40">📞 Place call</button>
          <button type="button" onClick={onPay} disabled={paying || !current || current.settled || current.subject_type === "order" || current.subject_type === "mandate"}
            title={current?.subject_type === "order" ? "A failed order is paid through its link or the store" : current?.subject_type === "mandate" ? "Nothing is owed yet" : "The customer pays the full amount"}
            className="px-4 py-2.5 rounded-lg bg-soft text-white text-sm font-semibold hover:bg-soft/90 disabled:opacity-40">Customer pays</button>
          {current?.self_serve_path && (
            <a href={current.self_serve_path} target="_blank" rel="noreferrer"
              className="px-4 py-2.5 rounded-lg border border-ink/20 text-sm hover:bg-ink/5">Customer's page ↗</a>
          )}

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" className="ml-auto inline-flex items-center gap-1.5 px-3.5 py-2.5 rounded-lg border border-ink/20 text-sm hover:bg-ink/5">
                Clock &amp; more <ChevronDown className="size-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-72">
              <DropdownMenuLabel className="text-xs text-ink/50">Move the clock (shared by every debt)</DropdownMenuLabel>
              <DropdownMenuItem disabled={busyClock} onSelect={() => onPlus(24)}>+1 day</DropdownMenuItem>
              <DropdownMenuItem disabled={busyClock} onSelect={() => onPlus(72)}>+3 days</DropdownMenuItem>
              <DropdownMenuItem disabled={busyClock} onSelect={onWindow}>Jump to 08:00 IST</DropdownMenuItem>
              <DropdownMenuItem disabled={busyClock} onSelect={onNextAny}>Next event for ANY debt</DropdownMenuItem>
              <DropdownMenuItem disabled={!clock?.shifted} onSelect={onResetClock}>Reset clock to real time</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuLabel className="text-xs text-ink/50">Calls</DropdownMenuLabel>
              <DropdownMenuItem onSelect={(e) => { e.preventDefault(); onInteractive(!state?.interactive_calls); }}>
                <span className="mr-2">{state?.interactive_calls ? "☑" : "☐"}</span> Scheduled calls ring in this browser
              </DropdownMenuItem>
              {persona && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem disabled={resetting} onSelect={onResetCustomer}>
                    {resetting ? "Resetting…" : `Reset ${persona.name}`}
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </section>
  );
}

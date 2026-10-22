import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import { chargeNext, syncMandate } from "@/lib/api/autopay";
import {
  checkLink, finishUnpaid, getQueue, promiseBroken,
  type DemoSubject, type DriveResult, type QueueItem,
} from "@/lib/api/demo";

import { inr } from "./format";

const TONE: Record<string, string> = { ok: "text-soft", info: "text-ink/60", warn: "text-declined", bad: "text-failed" };

function errText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    return e.message;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}

function Tag({ children, cls }: { children: React.ReactNode; cls: string }) {
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${cls}`}>{children}</span>;
}

function Row({ it, first }: { it: QueueItem; first: boolean }) {
  return (
    <li className={`rounded-lg border px-3 py-2 ${first ? "border-ember/50 bg-ember/5" : "border-ink/10"}`}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className={`text-[11px] font-semibold ${first ? "text-ember" : "text-ink/40"}`}>{first ? "NEXT" : "then"}</span>
        <span className="font-medium text-sm">{it.title}</span>
        <span className="text-xs text-ink/50 whitespace-nowrap">{it.runs_at_ist} IST</span>
        <span className="flex gap-1 ml-auto">
          {it.customer_facing && <Tag cls="bg-ink/5 text-ink/60">customer-facing</Tag>}
          {it.uses_real_razorpay && <Tag cls="bg-soft/15 text-soft">real Razorpay</Tag>}
          {it.played_result && <Tag cls="bg-indigo/10 text-indigo">played result</Tag>}
          {it.may_carry_link && <Tag cls="bg-sand text-ink/60">may carry a link</Tag>}
        </span>
      </div>
      {it.detail && <p className="text-xs text-ink/60 mt-0.5">{it.detail}</p>}
      {it.hint && <p className="text-xs text-ember mt-0.5">{it.hint}</p>}
      {it.warn && <p className="text-xs mt-1 rounded bg-declined/15 text-declined px-2 py-1">⚠ {it.warn}</p>}
    </li>
  );
}

/** One debt's confirmed next steps, soonest first, and the buttons that walk them. */
export function QueuePanel({ subject, onNote, onOpen }: {
  subject: DemoSubject;
  onNote: (s: string) => void;
  onOpen: (subjectId: string) => void;
}) {
  const qc = useQueryClient();
  const key = ["demo", "queue", subject.subject_type, subject.subject_id] as const;
  const q = useQuery({ queryKey: key, queryFn: () => getQueue(subject.subject_type, subject.subject_id), refetchInterval: 3000 });
  const [drive, setDrive] = useState<DriveResult | null>(null);
  const [linkNote, setLinkNote] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["demo"] });

  const finish = useMutation({
    mutationFn: () => finishUnpaid(subject.subject_type, subject.subject_id),
    onSuccess: (r) => { setDrive(r); onNote(`Customer never paid: ${r.steps.length} step(s) ran, no real link or debit was used.`); refresh(); },
    onError: (e) => onNote(errText(e)),
  });
  const broken = useMutation({
    mutationFn: () => promiseBroken(subject.subject_type, subject.subject_id),
    onSuccess: (r) => { setDrive(r); onNote(`Promise broken: ${r.steps.length} step(s) ran, no real link or debit was used.`); refresh(); },
    onError: (e) => onNote(errText(e)),
  });
  const check = useMutation({
    mutationFn: () => checkLink(subject.subject_type, subject.subject_id),
    onSuccess: (r) => {
      setLinkNote(!r.checked ? (r.note ?? "Nothing to check.")
        : r.applied?.length ? `Razorpay says ${r.razorpay_status}; ${r.applied.length} payment(s) applied.`
        : `Razorpay says the link is ${r.razorpay_status}; nothing new to apply. ${r.note ?? ""}`);
      refresh();
    },
    onError: (e) => setLinkNote(errText(e)),
  });

  const isAutopay = subject.subject_type === "mandate";
  const syncRz = useMutation({
    mutationFn: () => syncMandate(subject.subject_id),
    onSuccess: (r) => { setLinkNote(`Razorpay says the autopay is ${r.status}.`); refresh(); },
    onError: (e) => setLinkNote(errText(e)),
  });
  const queueNext = useMutation({
    mutationFn: () => chargeNext(subject.subject_id),
    onSuccess: () => { onNote("The next debit is queued. Press Next event to walk it."); refresh(); },
    onError: (e) => onNote(errText(e)),
  });

  const d = q.data;
  const items = d?.items ?? [];
  const busy = finish.isPending || broken.isPending;
  const done = subject.settled;
  const hasPromise = !!d?.subject.open_promise;

  return (
    <section className="rounded-2xl bg-white border border-ink/10 p-5 shadow-[0_20px_40px_-34px_rgba(234,88,12,0.5)] space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-[220px] flex-1">
          <h2 className="font-display font-semibold text-lg">Next steps</h2>
          <p className="text-xs text-ink/50">
            Confirmed steps in order. The list changes as time passes and as the customer pays, promises or goes quiet.
            {d && <> Now: {d.clock.now_ist} IST.</>}
          </p>
        </div>
        <span className="text-xs text-ink/50 rounded-full bg-sand/60 px-3 py-1">press <strong>Next event ▶</strong> above to run the first step</span>
      </div>

      {d?.follow_up && (
        <div className="rounded-xl border border-loyal/40 bg-loyal/10 px-3.5 py-3 flex flex-wrap items-center gap-3">
          <p className="text-sm flex-1 min-w-[220px]">
            <strong>A debit on this autopay failed</strong>, so the recovery (notice, retries, calls, promise) now runs on its own item:
            {" "}{d.follow_up.title} · {inr(d.follow_up.amount_inr)} · {d.follow_up.status.replace(/_/g, " ")}.
          </p>
          <button type="button" onClick={() => onOpen(d.follow_up!.subject_id)}
            className="px-3 py-2 rounded-md bg-ink text-cream text-sm font-semibold hover:bg-ink/90">Open the failed debit →</button>
        </div>
      )}

      {items.length === 0 ? (
        <p className="text-sm text-ink/60 rounded-lg bg-sand/50 px-3 py-2">
          {isAutopay
            ? done ? "This autopay is cancelled, so nothing is queued."
              : "No debit is queued right now. Use “Queue the next debit” below to make one, then press Next event."
            : done ? "Nothing is queued: this debt is settled." : "Nothing is queued. The debt is paused (a promise is open), waiting on a person, or finished."}
        </p>
      ) : (
        <ol className="space-y-2 relative">{items.slice(0, 8).map((it, i) => <Row key={it.id} it={it} first={i === 0} />)}</ol>
      )}
      {items.length > 8 && <p className="text-xs text-ink/50">…and {items.length - 8} more.</p>}

      {(d?.outlook?.length ?? 0) > 0 && (
        <div className="rounded-xl border border-ink/10 bg-cream/60 px-3.5 py-3 space-y-2">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-ink/50">What happens after the debit</p>
          <ul className="space-y-1.5">
            {d!.outlook.map((o) => (
              <li key={o.when} className="text-sm">
                <span className={`font-medium ${o.tone === "ok" ? "text-soft" : o.tone === "bad" ? "text-failed" : "text-ink/60"}`}>{o.when}: </span>
                <span className="text-ink/70">{o.text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-ink/5">
        {isAutopay && (
          <button type="button" disabled={queueNext.isPending || done || subject.status !== "active"} onClick={() => queueNext.mutate()}
            title="Queue the pre-debit notice and the next debit for this autopay (the debit result follows the Razorpay settings below)"
            className="px-3 py-2 rounded-md bg-ink text-cream text-sm font-semibold disabled:opacity-40">
            {queueNext.isPending ? "Queueing…" : "Queue the next debit"}
          </button>
        )}
        {!isAutopay && <button type="button" disabled={busy || done}
          onClick={() => { if (window.confirm("Play this debt to its end as if the customer never pays? Every link is a stand-in and every call goes unanswered.")) finish.mutate(); }}
          title="Keeps pressing Next event until nothing is left, using no real Razorpay link or debit"
          className="px-3 py-2 rounded-md border border-failed text-failed text-sm font-semibold disabled:opacity-40">
          {finish.isPending ? "Playing…" : "Customer never pays: finish this debt"}
        </button>}
        {hasPromise && (
          <button type="button" disabled={busy || done} onClick={() => broken.mutate()}
            className="px-3 py-2 rounded-md border border-declined text-declined text-sm font-semibold disabled:opacity-40">
            {broken.isPending ? "Playing…" : "Promise was broken"}
          </button>
        )}
        <button type="button" disabled={check.isPending || syncRz.isPending} onClick={() => (isAutopay ? syncRz : check).mutate()}
          title={isAutopay ? "Ask Razorpay what state this card autopay is in" : "Ask Razorpay whether this debt's payment link was paid (for setups with no webhook tunnel)"}
          className="px-3 py-2 rounded-md border border-ink/20 text-sm disabled:opacity-40">
          {check.isPending || syncRz.isPending ? "Checking…" : isAutopay ? "Check Razorpay now" : "Check payment now"}
        </button>
        {linkNote && <span className="text-xs text-ink/70">{linkNote}</span>}
      </div>

      {drive && (
        <div className="rounded-xl bg-cream border border-ink/10 p-3">
          <p className="text-sm font-semibold">
            {drive.steps.length} step(s) ran → <span className={drive.after.settled ? "text-soft" : "text-failed"}>{drive.after.status.replace(/_/g, " ")}</span>
            <span className="font-normal text-ink/60"> · {inr(drive.after.amount_inr)}</span>
          </p>
          <p className="text-[11px] text-ink/50">Real links used: {drive.real_links_spent} · real debits made: {drive.real_debits_made}{drive.stopped_at_cap ? " · stopped at the step cap" : ""}</p>
          <ol className="mt-2 space-y-1 text-xs">
            {drive.steps.map((s, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-ink/40 w-28 shrink-0">{s.at}</span>
                <span><span className={`font-medium ${TONE[s.tone] ?? ""}`}>{s.title}</span>{s.detail && <span className="text-ink/60"> — {s.detail}</span>}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

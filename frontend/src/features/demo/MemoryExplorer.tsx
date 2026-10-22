import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { getMemoryExplorer, probeMemory, type MemoryEpisode, type MemoryFact } from "@/lib/api/demo";

import { fmtIst } from "./format";

const POLL = 4000;

type Meta = { holds: string; written: string; read: string; consent: string; kept: string };

function Tier({
  code, title, count, meta, open, onToggle, children,
}: {
  code: string; title: string; count: string; meta: Meta; open: boolean; onToggle: () => void; children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl bg-white border border-ink/10 p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="size-8 shrink-0 rounded-lg bg-ink text-cream grid place-items-center text-xs font-semibold">{code}</span>
        <h3 className="font-display font-semibold text-lg">{title}</h3>
        <span className="px-2 py-0.5 rounded-full bg-ink/5 text-[11px] text-ink/60">{count}</span>
        <button type="button" onClick={onToggle} className="ml-auto px-3 py-1 rounded-md border border-ink/15 text-xs">
          {open ? "Hide what is stored" : "Show what is stored"}
        </button>
      </div>
      <dl className="grid sm:grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-2 text-xs">
        {([["Holds", meta.holds], ["Written", meta.written], ["Read", meta.read], ["Needs consent / kept", `${meta.consent} ${meta.kept}`]] as const).map(([k, v]) => (
          <div key={k}>
            <dt className="font-semibold uppercase tracking-wide text-ink/40 text-[10px]">{k}</dt>
            <dd className="text-ink/70">{v}</dd>
          </div>
        ))}
      </dl>
      {open && <div className="border-t border-ink/10 pt-3 space-y-2">{children}</div>}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-ink/50">{children}</p>;
}

function FactRow({ f }: { f: MemoryFact }) {
  const old = f.status === "superseded";
  return (
    <li className={`flex flex-wrap gap-x-2 text-sm ${old ? "text-ink/40 line-through" : ""}`}>
      <span className="text-ink/50 no-underline">{f.key.replace(/_/g, " ")}:</span>
      <span>{f.value}</span>
      <span className="text-[11px] text-ink/40">
        {old ? `replaced ${fmtIst(f.valid_to)}` : `confirmed ${fmtIst(f.last_confirmed)}`} · sure {Math.round(f.confidence * 100)}%
      </span>
    </li>
  );
}

function Episode({ e }: { e: MemoryEpisode }) {
  return (
    <li className="rounded-lg border border-ink/10 p-2 text-sm">
      <span className="text-xs text-ink/50">
        {fmtIst(e.started_at)} · {e.outcome ?? "no outcome"} · importance {e.importance.toFixed(1)}
        {e.summary_source && ` · summary by ${e.summary_source === "llm" ? "the model" : e.summary_source === "seed" ? "demo seed" : "rules"}`}
      </span>
      <p className="mt-0.5">{e.summary ?? <span className="text-ink/40">Summary not written yet.</span>}</p>
      {e.tone_arc && <p className="text-xs text-ink/50">mood: {e.tone_arc}</p>}
      {e.key_events.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-xs text-ink/60">{e.key_events.map((k, i) => <li key={i}>{k}</li>)}</ul>
      )}
    </li>
  );
}

/** Everything remembered about one customer, laid out tier by tier the way the architecture stores it. */
export function MemoryView({
  subject, name, onForget, forgetting, onBack,
}: {
  subject: { subject_type: string; subject_id: string };
  name: string;
  onForget: () => void;
  forgetting: boolean;
  onBack: () => void;
}) {
  const [q, setQ] = useState("");
  const [showOld, setShowOld] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const exQ = useQuery({
    queryKey: ["demo", "memory-explorer", subject.subject_id],
    queryFn: () => getMemoryExplorer(subject.subject_type, subject.subject_id),
    refetchInterval: POLL,
  });
  const probe = useMutation({ mutationFn: () => probeMemory(subject.subject_type, subject.subject_id, q) });
  const toggle = (k: string) => setOpen((o) => ({ ...o, [k]: !o[k] }));
  const setAll = (v: boolean) => setOpen(Object.fromEntries(["t0", "t1", "t2", "t3", "t4", "t5", "t6", "k"].map((k) => [k, v])));

  const d = exQ.data;
  const header = (
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" onClick={onBack} className="px-3 py-2 rounded-md border border-ink/20 text-sm">← Back to the console</button>
      <h2 className="font-display font-semibold text-xl">What the agent remembers about {name}</h2>
      <span className="ml-auto flex gap-2">
        <button type="button" onClick={() => setAll(true)} className="px-3 py-1.5 rounded-md border border-ink/15 text-xs">Expand all</button>
        <button type="button" onClick={() => setAll(false)} className="px-3 py-1.5 rounded-md border border-ink/15 text-xs">Collapse all</button>
      </span>
    </div>
  );
  if (!d) return <div className="space-y-4">{header}<Empty>Loading...</Empty></div>;
  if (!d.contact_id) return <div className="space-y-4">{header}<Empty>This debt has no contact on file, so nothing can be remembered.</Empty></div>;

  const t = d.tiers ?? { notes: 0, episodes: 0, chunks: 0, facts: 0, links: 0 };
  const facts = d.facts ?? [];
  const activeFacts = facts.filter((f) => f.status === "active");
  const shownFacts = showOld ? facts : activeFacts;
  const episodes = d.episodes ?? [];
  const windows = episodes.flatMap((e) => e.chunks.map((c) => ({ ...c, when: e.started_at })));
  const w = d.working;
  const full = !!d.memory_consent_current;
  const consentLine = full
    ? `Agreed to the current wording (${d.consent_version}). Tiers 1, 3, 4, 5 and 6 below are kept for ${d.retention_days} days.`
    : d.memory_consent
      ? `Agreed only to the older “short notes” wording (${d.consent_version ?? "v1"}): only the older notes (tier 2) are kept. Nothing else is stored until they agree to ${d.current_version} on their page.`
      : "Has not agreed to memory: nothing below the factual records (tier 2) is kept.";

  return (
    <div className="space-y-4">
      {header}
      <div className="rounded-xl bg-sand/70 border border-ink/10 px-4 py-3 text-sm space-y-1">
        <p>{consentLine}</p>
        <p className="text-xs text-ink/60">
          Each tier below is a separate store with its own owner, lifetime and read rule. “Written” says when it is filled, “Read”
          says when the agent is handed it. A model never writes memory: code does, into fixed fields.
        </p>
      </div>

      <Tier code="T0" title="Working memory - this call" count={w ? `${w.total_turns} turn(s) · ${w.status.replace("_", " ")}` : "no call yet"}
        open={!!open["t0"]} onToggle={() => toggle("t0")}
        meta={{ holds: "The conversation so far on the current (or latest) call. A call is capped at 6 customer turns.",
                written: "Live, one row per turn (voice_turns).", read: "Every turn: the last 8 turns go to the model.",
                consent: "None.", kept: "Stays with the call record." }}>
        {!w ? <Empty>No call yet.</Empty> : (
          <ul className="space-y-1 text-sm">
            {w.turns.map((x) => (
              <li key={x.turn_no}><span className="text-ink/40 text-xs">{x.speaker === "agent" ? "Agent" : "Customer"}</span> {x.text}</li>
            ))}
          </ul>
        )}
      </Tier>

      <Tier code="T1" title="Profile card (core memory)" count={`${(d.core_card ?? []).length} line(s)`}
        open={!!open["t1"]} onToggle={() => toggle("t1")}
        meta={{ holds: "A short always-on profile: the active facts plus the last call in one line. Computed, never stored, so it cannot go stale.",
                written: "Not written - rebuilt from T3 and T4.", read: `Every turn (the agent may cite its lines as c1, c2...). Whole memory block capped at ${d.memory_budget_chars} characters.`,
                consent: "Current wording.", kept: "Follows T3 / T4." }}>
        {(d.core_card ?? []).length === 0 ? <Empty>Empty.</Empty> : (
          <ul className="space-y-0.5 text-sm">{(d.core_card ?? []).map((c, i) => <li key={i}><span className="text-ink/40">c{i + 1}</span> {c}</li>)}</ul>
        )}
      </Tier>

      <Tier code="T2" title="Factual records - exact truth" count={`${(d.records ?? []).length} record(s)${t.notes ? ` · ${t.notes} older note(s)` : ""}`}
        open={!!open["t2"]} onToggle={() => toggle("t2")}
        meta={{ holds: "What our own records say about this debt: earlier calls and how they ended, promises kept or broken. Never a guess, never a vector search.",
                written: "By the normal system (calls, promises, payments).", read: "Every turn.",
                consent: "None - it is the debt's own history.", kept: "As long as the record." }}>
        {(d.records ?? []).length === 0 && (d.notes ?? []).length === 0 ? <Empty>Nothing yet.</Empty> : (
          <>
            <ul className="space-y-1 text-sm">{(d.records ?? []).map((r, i) => <li key={i}>📋 {r}</li>)}</ul>
            {(d.notes ?? []).length > 0 && (
              <div>
                <p className="text-xs text-ink/50 mt-2">Older short notes (from the earlier consent wording; they become facts when the customer agrees to the current one):</p>
                <ul className="space-y-0.5 text-sm">{(d.notes ?? []).map((n) => <li key={n.id}>🧠 <span className="text-ink/50">{n.kind.replace("_", " ")}:</span> {n.text}</li>)}</ul>
              </div>
            )}
          </>
        )}
      </Tier>

      <Tier code="T3" title="Semantic memory - durable facts" count={`${t.facts} active${facts.length > t.facts ? ` · ${facts.length - t.facts} replaced` : ""}`}
        open={!!open["t3"]} onToggle={() => toggle("t3")}
        meta={{ holds: "Things that stay true about the person: money arrives around the 5th, prefers evenings, needs gentle handling. One current value per kind.",
                written: "After a call, from a fixed list of kinds (a model fills it; rules if the model is down). A changed value replaces the old one, which is kept.",
                read: "The top 5 appear in the profile card (T1).", consent: "Current wording.", kept: `${d.retention_days} days; unconfirmed minor facts fade sooner.` }}>
        {shownFacts.length === 0 ? <Empty>None.</Empty> : <ul className="space-y-0.5">{shownFacts.map((f) => <FactRow key={f.id} f={f} />)}</ul>}
        {facts.some((f) => f.status === "superseded") && (
          <button type="button" className="text-[11px] underline text-ink/50" onClick={() => setShowOld(!showOld)}>
            {showOld ? "Hide" : "Show"} replaced values
          </button>
        )}
      </Tier>

      <Tier code="T4" title="Episodic memory - what happened in each call" count={`${t.episodes} call(s)`}
        open={!!open["t4"]} onToggle={() => toggle("t4")}
        meta={{ holds: "One entry per finished call: a 2-3 sentence summary, the mood, key events and how important it was.",
                written: "The entry when the call ends; the summary a moment later (model, or rules if it is down).",
                read: "The latest one is in the profile card; others when the customer asks about the past (“pichli baar...”).",
                consent: "Current wording.", kept: `${d.retention_days} days.` }}>
        {episodes.length === 0 ? <Empty>No call kept yet.</Empty> : <ul className="space-y-2">{episodes.map((e) => <Episode key={e.id} e={e} />)}</ul>}
      </Tier>

      <Tier code="T5" title="Verbatim archive - the conversation word for word" count={`${t.chunks} window(s)`}
        open={!!open["t5"]} onToggle={() => toggle("t5")}
        meta={{ holds: "Every call exactly as spoken, in 4-turn windows that overlap by one turn. Not summarised, not edited.",
                written: "When the call ends - no model involved. Embedded so it can be searched.",
                read: `Only on a recall question: vector + keyword search, scored by relevance x recency (${d.half_life_days}-day half-life) x importance.`,
                consent: "Current wording.", kept: `${d.retention_days} days.` }}>
        {windows.length === 0 ? <Empty>Nothing kept.</Empty> : (
          <ul className="space-y-1.5">
            {windows.map((c) => (
              <li key={c.id}>
                <pre className="whitespace-pre-wrap rounded-md bg-ink/5 p-2 text-xs">
                  <span className="text-ink/40">{fmtIst(c.when)} · turns {c.turn_from}-{c.turn_to}{c.embedded ? "" : " · not embedded"}{"\n"}</span>
                  {c.text}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </Tier>

      <Tier code="T6" title="Links - how the items relate" count={`${t.links} link(s)`}
        open={!!open["t6"]} onToggle={() => toggle("t6")}
        meta={{ holds: "Typed edges between memory items: temporal_next (what came before), entity (the debt or promise), causal (a broken promise led to this call), supersedes.",
                written: "By code, from records - never by the model.", read: "After a recall: one hop from the matched call.",
                consent: "Follows the item.", kept: "Follows the item." }}>
        {(d.links ?? []).length === 0 ? <Empty>None.</Empty> : (
          <ul className="text-xs text-ink/70 space-y-0.5">
            {(d.links ?? []).map((l, i) => <li key={i}>{l.src} <span className="font-semibold">{l.relation}</span> {l.dst}</li>)}
          </ul>
        )}
      </Tier>

      <Tier code="K" title="Knowledge store - the merchant's documents (separate)" count={`${d.knowledge?.approved_passages ?? 0} approved passage(s)`}
        open={!!open["k"]} onToggle={() => toggle("k")}
        meta={{ holds: "Policies and how-tos the merchant approved. Shared by every customer and never mixed with anything above, so one customer's words can never leak into another's answer.",
                written: "By the merchant, then approved (Call Knowledge tab).", read: "Only for a policy question; the agent repeats approved text.",
                consent: "None.", kept: "Until the merchant withdraws it." }}>
        <p className="text-sm text-ink/70">{d.knowledge?.docs ?? 0} approved document(s) · {d.knowledge?.approved_passages ?? 0} passage(s). Manage them in the dashboard's Call Knowledge tab.</p>
      </Tier>

      <section className="rounded-2xl bg-white border border-ink/10 p-4 space-y-3">
        <h3 className="font-display font-semibold">Try a sentence</h3>
        <p className="text-xs text-ink/50">What the agent would be handed if the customer said this. Past-tense wording (“pichli baar...”, “last time...”) triggers a recall.</p>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (q.trim()) probe.mutate(); }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. pichli baar maine kya kaha tha?"
            className="flex-1 rounded-md border border-ink/15 px-3 py-1.5 text-sm" />
          <button type="submit" disabled={!q.trim() || probe.isPending}
            className="px-3 py-1.5 rounded-md bg-ink text-cream text-sm font-semibold disabled:opacity-50">Probe</button>
        </form>
        {probe.data && (
          <div className="space-y-1.5 text-sm">
            <p className="text-xs text-ink/60">
              {probe.data.full_consent ? (probe.data.cue ? "Recall triggered." : "No recall wording: only the profile card is used.") : "No memory is used for this customer."}
            </p>
            {probe.data.recalled.map((r) => (
              <div key={r.mid} className="rounded-md bg-ink/5 p-2">
                <span className="text-xs text-ink/50">{r.mid} · {r.kind} · call of {r.when} · score {r.score.toFixed(2)}</span>
                <pre className="whitespace-pre-wrap text-xs mt-0.5">{r.text}</pre>
              </div>
            ))}
          </div>
        )}
        {(d.recent_turns ?? []).length > 0 && (
          <div className="space-y-1.5">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-ink/50">Recent answers that read memory</h4>
            <ul className="space-y-1.5 text-sm">
              {(d.recent_turns ?? []).map((r) => (
                <li key={`${r.call_id}-${r.turn_no}`} className="rounded-md bg-ink/5 p-2">
                  <span className="text-xs text-ink/50">
                    {r.kind ?? "reply"} · {r.memory.cue ? "recall" : "profile only"}
                    {r.sources?.length ? ` · from ${r.sources.map((s) => s.title).join(", ")}` : ""}
                  </span>
                  <p>{r.text}</p>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <div className="flex justify-end">
        <button type="button" disabled={forgetting || Object.values(t).reduce((a, b) => a + b, 0) === 0}
          onClick={() => { if (window.confirm("Delete everything remembered about this customer (all tiers)?")) onForget(); }}
          className="px-3 py-2 rounded-md border border-ember text-ember text-sm disabled:opacity-40">Forget everything about this customer</button>
      </div>
    </div>
  );
}

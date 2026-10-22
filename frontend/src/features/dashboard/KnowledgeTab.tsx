import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addKnowledgeDoc, approveKnowledgeDoc, deleteKnowledgeDoc, getKnowledgeDoc, getKnowledgeStatus,
  listKnowledgeDocs, loadSamplePolicy, reembedKnowledge, searchKnowledge, unapproveKnowledgeDoc, uploadKnowledgeFile,
  type SearchResult,
} from "@/lib/api/knowledge";

import { ToneBadge } from "./components/ToneBadge";

function msg(e: unknown) {
  return e instanceof Error ? e.message : "Something went wrong";
}

export function KnowledgeTab() {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<SearchResult | null>(null);
  const [error, setError] = useState("");

  const statusQ = useQuery({ queryKey: ["knowledge", "status"], queryFn: getKnowledgeStatus, refetchInterval: 8000 });
  const docsQ = useQuery({ queryKey: ["knowledge", "docs"], queryFn: listKnowledgeDocs, refetchInterval: 8000 });
  const detailQ = useQuery({
    queryKey: ["knowledge", "doc", open], queryFn: () => getKnowledgeDoc(open!), enabled: !!open,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["knowledge"] });
  const onErr = (e: unknown) => setError(msg(e));
  const ok = () => { setError(""); void refresh(); };

  const add = useMutation({ mutationFn: () => addKnowledgeDoc(title || "Untitled", body), onSuccess: () => { setTitle(""); setBody(""); ok(); }, onError: onErr });
  const sample = useMutation({ mutationFn: loadSamplePolicy, onSuccess: ok, onError: onErr });
  const approve = useMutation({ mutationFn: approveKnowledgeDoc, onSuccess: ok, onError: onErr });
  const unapprove = useMutation({ mutationFn: unapproveKnowledgeDoc, onSuccess: ok, onError: onErr });
  const remove = useMutation({ mutationFn: deleteKnowledgeDoc, onSuccess: ok, onError: onErr });
  const upload = useMutation({ mutationFn: (f: File) => uploadKnowledgeFile(f), onSuccess: ok, onError: onErr });
  const reembed = useMutation({ mutationFn: reembedKnowledge, onSuccess: ok, onError: onErr });
  const search = useMutation({ mutationFn: () => searchKnowledge(q), onSuccess: (r) => { setFound(r); setError(""); }, onError: onErr });

  const st = statusQ.data;
  const stale = Object.values(st?.stale_vectors ?? {}).reduce((a, b) => a + b, 0);
  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-ink/10 bg-white p-4">
        <h2 className="font-display font-semibold">What the call agent may say about your business</h2>
        <p className="text-sm text-ink/60 mt-1">
          Add your policies and how-tos. The agent answers customers&apos; questions (late fees, refunds, payment methods, changing a
          bank account, disputes, support hours...) <strong>only from documents you approve</strong>, repeats what they say instead of
          inventing policy, and hands over to a person when nothing matches.
        </p>
        {st && (
          <p className="text-xs text-ink/50 mt-2">
            {st.approved_passages} approved passage(s) · search: {st.vector_engine} · {st.embeddings ? "embeddings on" : "keyword search only (embedding model not loaded)"} · model {st.embedding_model.split("/").pop()}
            {stale > 0 && (
              <>
                {" "}· {stale} vector(s) made by another model{" "}
                <button type="button" onClick={() => reembed.mutate()} disabled={reembed.isPending}
                  className="underline font-semibold">Re-embed now</button>
              </>
            )}
          </p>
        )}
      </div>

      {error && <p className="text-sm text-failed">{error}</p>}

      <div className="grid lg:grid-cols-2 gap-6">
        <div className="rounded-xl border border-ink/10 bg-white p-4 space-y-3">
          <h3 className="text-sm font-semibold">Add a document</h3>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (e.g. Refund and late fee policy)"
            className="w-full rounded-md border border-ink/15 px-3 py-2 text-sm" />
          <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={8}
            placeholder={"Paste text. Use headings (## Refunds) to split it into passages. Add a line like\nAlso asked as: refund kaise milega; paisa wapas kab aayega\nunder a passage to help customers' own wording find it."}
            className="w-full rounded-md border border-ink/15 px-3 py-2 text-sm font-mono" />
          <div className="flex flex-wrap gap-2 items-center">
            <button type="button" onClick={() => add.mutate()} disabled={!body.trim() || add.isPending}
              className="px-3 py-1.5 rounded-md bg-ink text-cream text-sm font-semibold disabled:opacity-50">Add as draft</button>
            <label className="px-3 py-1.5 rounded-md border border-ink/15 text-sm cursor-pointer">
              Upload .txt / .md / .pdf
              <input type="file" accept=".txt,.md,.pdf" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
            </label>
            <button type="button" onClick={() => sample.mutate()} disabled={sample.isPending}
              className="px-3 py-1.5 rounded-md border border-ink/15 text-sm">Load sample store policy</button>
          </div>
          <p className="text-xs text-ink/50">New documents start as <strong>draft</strong> and are never spoken until you approve them.</p>
        </div>

        <div className="rounded-xl border border-ink/10 bg-white p-4 space-y-3">
          <h3 className="text-sm font-semibold">Try a question</h3>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (q.trim()) search.mutate(); }}>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. refund kitne din mein aata hai?"
              className="flex-1 rounded-md border border-ink/15 px-3 py-2 text-sm" />
            <button type="submit" disabled={!q.trim() || search.isPending}
              className="px-3 py-1.5 rounded-md bg-ink text-cream text-sm font-semibold disabled:opacity-50">Search</button>
          </form>
          {found && (
            <div className="space-y-2">
              <p className="text-xs text-ink/60">
                {found.answerable ? "The agent could answer from these" : "Nothing matches well enough: the agent would hand this to a person"}
                {" "}· {found.ms} ms · a passage needs ≥ {found.min_score} similarity
              </p>
              {found.hits.length === 0 && <p className="text-sm text-ink/50">No approved passages found.</p>}
              {found.hits.map((h) => (
                <div key={h.id} className="rounded-lg border border-ink/10 p-2 text-sm">
                  <div className="flex justify-between text-xs text-ink/50">
                    <span>{h.heading ?? h.title}</span>
                    <span className={h.score >= found.min_score ? "text-soft" : ""}>{h.score.toFixed(2)}</span>
                  </div>
                  <p className="mt-1">{h.text}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="rounded-xl border border-ink/10 bg-white p-4">
        <h3 className="text-sm font-semibold mb-2">Your documents</h3>
        {(docsQ.data ?? []).length === 0 && <p className="text-sm text-ink/50">None yet. Load the sample policy to try it.</p>}
        <ul className="divide-y divide-ink/10">
          {(docsQ.data ?? []).map((d) => (
            <li key={d.id} className="py-3">
              <div className="flex flex-wrap items-center gap-3">
                <button type="button" className="font-medium text-left hover:underline" onClick={() => setOpen(open === d.id ? null : d.id)}>
                  {d.title}
                </button>
                <ToneBadge label={d.status === "approved" ? "Approved" : d.status === "superseded" ? "Replaced" : "Draft"}
                  tone={d.status === "approved" ? "soft" : "neutral"} />
                <span className="text-xs text-ink/50">v{d.version ?? 1} · {d.chunks} passage(s) · {d.source}</span>
                <span className="ml-auto flex gap-2">
                  {d.status !== "approved" ? (
                    <button type="button" onClick={() => approve.mutate(d.id)} className="px-2.5 py-1 rounded-md bg-soft text-white text-xs font-semibold">
                      {d.status === "superseded" ? "Restore this version" : "Approve"}
                    </button>
                  ) : (
                    <button type="button" onClick={() => unapprove.mutate(d.id)} className="px-2.5 py-1 rounded-md border border-ink/15 text-xs">Withdraw</button>
                  )}
                  <button type="button" onClick={() => { if (window.confirm(`Delete "${d.title}"?`)) remove.mutate(d.id); }}
                    className="px-2.5 py-1 rounded-md border border-ember text-ember text-xs">Delete</button>
                </span>
              </div>
              {open === d.id && detailQ.data && (
                <ol className="mt-2 space-y-2 text-sm">
                  {detailQ.data.passages.map((p) => (
                    <li key={p.id} className="rounded-md bg-ink/5 p-2">
                      {p.heading && <span className="text-xs font-semibold text-ink/60">{p.heading}</span>}
                      <p>{p.text}</p>
                    </li>
                  ))}
                </ol>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

import { apiFetch } from "./client";
import { getToken } from "../auth-token";
import { getMerchantKey } from "../merchant-key";

export type KnowledgeStatus = {
  embeddings: boolean; embedding_model: string; vector_engine: string; min_score: number;
  docs: number; approved_passages: number;
  /** Vectors made by a different embedding model than the current one (or never made), per table. */
  stale_vectors?: Record<string, number>;
};
export type KnowledgeDoc = {
  id: string; title: string; source: string; status: "draft" | "approved" | "superseded"; chunks: number;
  created_at: string; approved_at: string | null; version?: number; supersedes_id?: string | null;
};
export type KnowledgeDocDetail = KnowledgeDoc & { passages: { id: string; heading: string | null; text: string; section?: string | null }[] };
export type SearchHit = { id: string; title: string; heading: string | null; text: string; score: number };
export type SearchResult = { ms: number; min_score: number; answerable: boolean; hits: SearchHit[] };

export const getKnowledgeStatus = () => apiFetch<KnowledgeStatus>("/knowledge/status");
export const listKnowledgeDocs = () => apiFetch<KnowledgeDoc[]>("/knowledge/docs");
export const getKnowledgeDoc = (id: string) => apiFetch<KnowledgeDocDetail>(`/knowledge/docs/${id}`);
export const addKnowledgeDoc = (title: string, text: string) =>
  apiFetch<KnowledgeDoc>("/knowledge/docs", { method: "POST", body: { title, text } });
export const approveKnowledgeDoc = (id: string) => apiFetch<KnowledgeDoc>(`/knowledge/docs/${id}/approve`, { method: "POST" });
export const unapproveKnowledgeDoc = (id: string) => apiFetch<KnowledgeDoc>(`/knowledge/docs/${id}/unapprove`, { method: "POST" });
export const deleteKnowledgeDoc = (id: string) => apiFetch<{ deleted: string }>(`/knowledge/docs/${id}`, { method: "DELETE" });
export const reembedKnowledge = () => apiFetch<Record<string, unknown>>("/knowledge/reembed", { method: "POST" });
export const loadSamplePolicy = () => apiFetch<KnowledgeDoc>("/knowledge/sample", { method: "POST" });
export const searchKnowledge = (query: string) =>
  apiFetch<SearchResult>("/knowledge/search", { method: "POST", body: { query } });

/** A .txt / .md / .pdf file sent as the raw request body (no multipart). */
export async function uploadKnowledgeFile(file: File): Promise<KnowledgeDoc> {
  const key = getMerchantKey();
  const token = getToken();
  const res = await fetch(`/knowledge/docs/upload?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      ...(key ? { "X-Merchant-Key": key } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: file,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.detail) || `Upload failed (${res.status})`);
  return data as KnowledgeDoc;
}

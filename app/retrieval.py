"""
Retrieval for the call agent: two stores, one search surface.

  search_knowledge  - the merchant's APPROVED passages (shared by all their customers)
  search_memory     - notes about ONE customer (never crosses contacts)
  search_both       - both, in parallel, each on its own DB session

Each lookup is hybrid: vector similarity plus full-text, merged with reciprocal-rank fusion,
because pure vectors blur invoice numbers and exact terms while pure keywords miss
paraphrases. `Hit.score` is the cosine similarity of the best vector match - that, not the
fused rank, is what the "is this relevant enough to answer from?" gate compares with
RETRIEVAL_MIN_SCORE. If embeddings are unavailable it falls back to word overlap.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, embeddings, vectorstore
from app.config import settings
from app.database import SessionLocal

RRF_K = 60


@dataclass
class Hit:
    id: str
    text: str
    score: float                      # cosine similarity (or word overlap when no embeddings)
    title: str = ""
    kind: str = ""
    extra: dict = field(default_factory=dict)


def _overlap(query: str, passage: str) -> float:
    q = set(re.findall(r"[\wऀ-ॿ]{3,}", (query or "").lower())) - vectorstore._STOP
    if not q:
        return 0.0
    p = set(re.findall(r"[\wऀ-ॿ]{3,}", (passage or "").lower()))
    return len(q & p) / len(q)


def _hybrid(db: Session, table: str, where: str, params: dict, query: str, k: int,
            text_expr: str = "text") -> list[tuple[str, float | None, float]]:
    """[(id, cosine or None when only the keyword search found it, rrf)] best first."""
    cos: dict[str, float] = {}
    ranks: dict[str, float] = {}
    if embeddings.available():
        try:
            for r, (i, s) in enumerate(vectorstore.vector_search(db, table, where, params, embeddings.embed_query(query), k * 3)):
                cos[i] = s
                ranks[i] = ranks.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
        except Exception:
            db.rollback()
    for r, (i, _s) in enumerate(vectorstore.fts_search(db, table, where, params, query, k * 3, text_expr)):
        ranks[i] = ranks.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
    ordered = sorted(ranks, key=lambda i: -ranks[i])[:k]
    return [(i, cos.get(i), ranks[i]) for i in ordered]


def is_answerable(hit: "Hit") -> bool:
    """Is this hit worth handing to the model as a policy answer? Cosine floor, plus a lexical or strong-semantic confirmation."""
    if hit is None or hit.score < settings.retrieval_min_score:
        return False
    return (hit.extra or {}).get("overlap", 1.0) >= settings.retrieval_min_overlap or hit.score >= settings.retrieval_confident_score


def search_knowledge(db: Session, query: str, merchant_id: str | None = None, k: int = 4) -> list[Hit]:
    m = merchant_id or settings.default_merchant_id
    where, params = "merchant_id = :m AND status = 'approved'", {"m": m}
    # keyword side searches the passage, its "Also asked as" lines AND its Hinglish / Hindi wording (a Roman-Hinglish question
    # otherwise only matches by meaning: keyword-only recall went from 64 % to 88 % with the translations included)
    found = _hybrid(db, "knowledge_chunks", where, params, query, k,
                    "text || ' ' || coalesce(aliases, '') || ' ' || coalesce(translations, '')")
    if not found:
        return []
    rows = db.execute(text(
        "SELECT c.id, c.text, c.heading, d.title, d.id, c.aliases, c.section_text, c.translations FROM knowledge_chunks c JOIN knowledge_docs d ON d.id = c.doc_id "
        "WHERE c.id = ANY(CAST(:ids AS uuid[]))"), {"ids": [i for i, _, _ in found]}).all()
    by = {str(r[0]): r for r in rows}
    out = []
    for i, cosine, _rrf in found:
        r = by.get(i)
        if r is None:
            continue
        score = cosine if cosine is not None else _overlap(query, f"{r[1]} {r[5] or ''}")
        out.append(Hit(id=i, text=r[1], score=score, title=r[3] or "", kind="knowledge",
                       extra={"heading": r[2], "doc_id": str(r[4]), "section": r[6],
                              "translations": json.loads(r[7]) if r[7] else {},
                              "overlap": _overlap(query, f"{r[1]} {r[5] or ''} {r[7] or ''}")}))
    return out


def search_memory(db: Session, contact_id: str, query: str, merchant_id: str | None = None, k: int = 3,
                  now: datetime | None = None) -> list[Hit]:
    """Notes about this contact only (the contact filter is in the SQL, not applied afterwards)."""
    m = merchant_id or settings.default_merchant_id
    where = "merchant_id = :m AND contact_id = CAST(:c AS uuid) AND expires_at > :now"
    params = {"m": m, "c": contact_id, "now": now or clock.now()}
    found = _hybrid(db, "customer_memory", where, params, query, k)
    if not found:
        return []
    rows = db.execute(text("SELECT id, text, kind FROM customer_memory WHERE id = ANY(CAST(:ids AS uuid[]))"),
                      {"ids": [i for i, _, _ in found]}).all()
    by = {str(r[0]): r for r in rows}
    return [Hit(id=i, text=by[i][1], score=(c if c is not None else _overlap(query, by[i][1])), kind=by[i][2])
            for i, c, _ in found if i in by]


def search_both(query: str, contact_id: str | None, merchant_id: str | None = None,
                k_knowledge: int = 4, k_memory: int = 3) -> dict:
    """Both stores at once; each lookup opens its own session (a Session is not thread-safe)."""
    def run(fn, *a):
        s = SessionLocal()
        try:
            return fn(s, *a)
        finally:
            s.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fk = pool.submit(run, search_knowledge, query, merchant_id, k_knowledge)
        fm = pool.submit(run, search_memory, contact_id, query, merchant_id, k_memory) if contact_id else None
        return {"knowledge": fk.result(), "memory": fm.result() if fm else []}

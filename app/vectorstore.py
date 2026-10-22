"""
Where vectors are searched. One tiny surface, two engines:

- pgvector (when the extension is installed): `embedding::vector(N)` cosine distance with an
  HNSW expression index, tenant/contact filters in the same SQL. Built for scale - roughly
  logarithmic search time, filtered by merchant, in the database the data already lives in
  (no extra network hop, and deleting a customer's memory is one transaction).
- in process (always available): numpy cosine over the rows that match the filter. Fine up to
  a few thousand rows, which is what a demo or a single small merchant has. The app never
  fails because an extension is missing.

Callers use `vector_search` and `fts_search` only, so a managed vector database could replace
this module without touching the call code.
"""
import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

_state: dict = {"pgvector": False}

# Words that carry no search signal in Hinglish / English questions.
_STOP = {"kya", "hai", "hain", "ka", "ki", "ke", "ko", "se", "me", "mein", "mai", "main", "aap", "ap", "toh", "to",
         "the", "is", "are", "a", "an", "of", "to", "do", "i", "my", "me", "you", "your", "can", "will", "how",
         "what", "kaise", "kitna", "koi", "ye", "yeh", "woh", "wo", "bhi", "ho", "hoon", "kar", "karna", "karo"}


def pgvector_active() -> bool:
    return bool(_state["pgvector"])


def init(engine) -> bool:
    """Enable pgvector + HNSW indexes if the extension can be created. Safe to call
    on every start; never raises."""
    dim = settings.embedding_dim
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            for table, name in (("knowledge_chunks", "knowledge_chunks_hnsw"), ("customer_memory", "customer_memory_hnsw"),
                                ("episode_chunks", "episode_chunks_hnsw"), ("customer_facts", "customer_facts_hnsw")):
                c.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
                    f"USING hnsw ((embedding::vector({dim})) vector_cosine_ops)"))
        _state["pgvector"] = True
        logger.info("pgvector enabled (HNSW indexes ready)")
    except Exception as e:
        _state["pgvector"] = False
        logger.info("pgvector not available (%s); using in-process vector search", type(e).__name__)
    return _state["pgvector"]


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def vector_search(db: Session, table: str, where: str, params: dict, qvec: list[float], k: int) -> list[tuple[str, float]]:
    """[(id, cosine similarity)] best first. `where` is a trusted SQL fragment (never user text)."""
    dim = settings.embedding_dim
    if _state["pgvector"]:
        try:
            db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))   # keep filling the result under a filter (pgvector >= 0.8)
        except Exception:
            db.rollback()
        rows = db.execute(text(
            f"SELECT id, 1 - ((embedding::vector({dim})) <=> CAST(:q AS vector({dim}))) AS score "
            f"FROM {table} WHERE {where} AND embedding IS NOT NULL "
            f"ORDER BY (embedding::vector({dim})) <=> CAST(:q AS vector({dim})) LIMIT :k"),
            {**params, "q": _vec_literal(qvec), "k": k}).all()
        return [(str(r[0]), float(r[1])) for r in rows]
    import numpy as np
    rows = db.execute(text(f"SELECT id, embedding FROM {table} WHERE {where} AND embedding IS NOT NULL"), params).all()
    if not rows:
        return []
    mat = np.array([r[1] for r in rows], dtype="float32")
    scores = mat @ np.array(qvec, dtype="float32")        # unit vectors: dot == cosine
    order = np.argsort(-scores)[:k]
    return [(str(rows[i][0]), float(scores[i])) for i in order]


def _tsquery(q: str) -> str | None:
    toks = [t for t in re.findall(r"[\wऀ-ॿ]{3,}", (q or "").lower()) if t not in _STOP]
    return " | ".join(dict.fromkeys(toks)) or None


def fts_search(db: Session, table: str, where: str, params: dict, query: str, k: int,
               text_expr: str = "text") -> list[tuple[str, float]]:
    """Lexical search (Postgres full text, OR of the meaningful words). Catches what vectors
    blur: invoice numbers, exact terms, Hinglish spellings that appear verbatim."""
    tq = _tsquery(query)
    if not tq:
        return []
    rows = db.execute(text(
        f"SELECT id, ts_rank(to_tsvector('simple', {text_expr}), to_tsquery('simple', :tq)) AS r "
        f"FROM {table} WHERE {where} AND to_tsvector('simple', {text_expr}) @@ to_tsquery('simple', :tq) "
        f"ORDER BY r DESC LIMIT :k"), {**params, "tq": tq, "k": k}).all()
    return [(str(r[0]), float(r[1])) for r in rows]

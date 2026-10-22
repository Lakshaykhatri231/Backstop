"""
The merchant's knowledge store (RAG #1).

The merchant adds policy / how-to text (paste, .txt, .md, .pdf). It is split into
passages, embedded, and stored as DRAFT. Nothing is ever spoken from a passage until the
merchant APPROVES its document - the agent repeats approved text, it does not invent
policy. Approval, deletion and every addition are audited.

Documents are versioned by title: adding a changed document under the same title makes version N+1
(the old one stays until the new one is approved, then becomes `superseded`, kept for history and for
rolling back); an identical re-upload is refused. Every vector remembers which embedding model made it,
and `reembed_stale` re-embeds anything made by a different model (it refuses a change of dimension,
which needs the HNSW indexes rebuilt). A passage that is one piece of a longer section also stores the
whole section (`section_text`), so a small hit hands the model its full context.
"""
import hashlib
import io
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func as sql_func, text as sql
from sqlalchemy.orm import Session

from app import clock, embeddings, retrieval, vectorstore
from app.audit import write_audit_entry
from app.config import settings
from app.database import get_db
from app.models import gen_uuid
from app.rag_models import ConversationEpisode, CustomerFact, EpisodeChunk, KnowledgeChunk, KnowledgeDoc

router = APIRouter(prefix="/knowledge")

MAX_DOC_CHARS = 200_000
CHUNK_TARGET = 420


# ── Chunking ─────────────────────────────────────────────────────────────────

def _sentences(s: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?।])\s+", s) if p.strip()]


_ALIAS = re.compile(r"^\s*(?:also asked as|asked as|aliases?|questions?)\s*:\s*(.+\S)\s*$", re.IGNORECASE)
# "Hinglish: ..." / "Hindi: ..." under a passage: the same policy in the language of the call. Spoken when the agent has to
# read the approved text itself (its own reply failed the check); the English passage stays the grounding for numbers.
_TRANSLATION = re.compile(r"^\s*(hinglish|hindi)\s*:\s*(.+\S)\s*$", re.IGNORECASE)
_TR_KEY = {"hinglish": "hinglish", "hindi": "hi"}


class Chunk(tuple):
    """(heading, passage, aliases) - a plain 3-tuple everywhere it is unpacked - plus `.translations`."""
    translations: dict

    def __new__(cls, heading, text, aliases, translations=None):
        obj = super().__new__(cls, (heading, text, aliases))
        obj.translations = dict(translations or {})
        return obj


def chunk_text(body: str, target: int = CHUNK_TARGET) -> list[Chunk]:
    """[(heading, passage, aliases)]. Splits on markdown headings and blank lines; a "Q:"/"A:" pair
    stays together; long sections are cut on sentence boundaries. A line "Also asked as: ..." is
    pulled out as `aliases`: other ways customers phrase the question (Hinglish spellings, synonyms),
    searched together with the passage but never spoken."""
    out: list[Chunk] = []
    aliases: list[str] = []
    translations: dict[str, str] = {}
    heading: str | None = None
    para: list[str] = []

    def flush():
        nonlocal para, aliases, translations
        block = " ".join(x.strip() for x in para if x.strip())
        para = []
        al = " ; ".join(aliases)
        tr = dict(translations)
        aliases = []
        translations = {}
        if not block:
            return
        if len(block) <= target:
            out.append(Chunk(heading, block, al, tr))
            return
        sents, cur = _sentences(block), ""
        for s in sents:
            if cur and len(cur) + len(s) + 1 > target:
                out.append(Chunk(heading, cur, al, tr))
                cur = s
                continue
            cur = f"{cur} {s}".strip()
        if cur:
            out.append(Chunk(heading, cur, al, tr))

    in_comment = False
    for line in body.splitlines():
        # <!-- notes for the merchant --> are never part of a passage, so they can never be spoken to a customer
        if in_comment:
            in_comment = "-->" not in line
            continue
        if line.strip().startswith("<!--"):
            in_comment = "-->" not in line
            continue
        am = _ALIAS.match(line)
        if am:
            aliases.append(am.group(1).strip())
            continue
        tm = _TRANSLATION.match(line)
        if tm:
            translations[_TR_KEY[tm.group(1).lower()]] = tm.group(2).strip()
            continue
        m = re.match(r"^\s{0,3}#{1,4}\s+(.*\S)\s*$", line)
        if m:
            flush()
            heading = m.group(1).strip()
            continue
        if not line.strip():
            # keep a question with its answer: do not split between "Q:" and "A:"
            if para and re.match(r"^\s*Q[:.]", para[0]) and not any(re.match(r"^\s*A[:.]", p) for p in para):
                continue
            flush()
            continue
        para.append(line)
    flush()
    # a heading with a short body is better retrieved WITH its heading
    return [Chunk(c[0], f"{c[0]}. {c[1]}" if c[0] and not c[1].lower().startswith(c[0].lower()) else c[1], c[2], c.translations)
            for c in out]


def extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            return "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        except Exception as e:
            raise HTTPException(422, f"Could not read that PDF ({type(e).__name__}).")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


# ── Store operations ─────────────────────────────────────────────────────────

def content_hash(body: str) -> str:
    return hashlib.sha256(" ".join((body or "").split()).encode("utf-8")).hexdigest()


def section_texts(chunks: list[tuple[str | None, str, str]]) -> list[str | None]:
    """For each passage, the whole section it belongs to - only when the section was split into several
    passages (otherwise the passage already IS the section)."""
    by: dict[str | None, list[int]] = {}
    for i, (h, _t, _a) in enumerate(chunks):
        by.setdefault(h, []).append(i)
    out: list[str | None] = [None] * len(chunks)
    for h, idx in by.items():
        if len(idx) < 2:
            continue
        parts = [chunks[idx[0]][1]] + [(chunks[k][1][len(h) + 2:] if h and chunks[k][1].startswith(f"{h}. ") else chunks[k][1])
                                       for k in idx[1:]]
        whole = " ".join(parts)
        for k in idx:
            out[k] = whole
    return out


def _supersede_older(db: Session, doc: KnowledgeDoc) -> int:
    """Approving a document retires any other APPROVED version with the same title (kept, not deleted)."""
    older = (db.query(KnowledgeDoc).filter(KnowledgeDoc.merchant_id == doc.merchant_id, KnowledgeDoc.id != doc.id,
                                           KnowledgeDoc.status == "approved")
             .filter(sql_func.lower(KnowledgeDoc.title) == doc.title.lower()).all())
    for o in older:
        o.status = "superseded"
        db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == o.id).update({"status": "superseded"})
        write_audit_entry(db, "knowledge_doc_superseded", {"doc_id": o.id, "by_doc_id": doc.id, "title": o.title,
                                                           "version": o.version})
    return len(older)


def add_document(db: Session, title: str, body: str, source: str = "pasted", merchant_id: str | None = None,
                 approved: bool = False) -> KnowledgeDoc:
    body = (body or "").strip()
    if not body:
        raise HTTPException(422, "The document is empty.")
    if len(body) > MAX_DOC_CHARS:
        raise HTTPException(413, "That document is too large (200,000 characters max).")
    m = merchant_id or settings.default_merchant_id
    title = (title or "Untitled")[:200]
    digest = content_hash(body)
    same = (db.query(KnowledgeDoc).filter(KnowledgeDoc.merchant_id == m, KnowledgeDoc.content_hash == digest,
                                          KnowledgeDoc.status != "superseded").first())
    if same is not None:
        raise HTTPException(409, f'This exact text is already in your documents ("{same.title}", version {same.version}).')
    previous = (db.query(KnowledgeDoc).filter(KnowledgeDoc.merchant_id == m)
                .filter(sql_func.lower(KnowledgeDoc.title) == title.lower())
                .order_by(KnowledgeDoc.version.desc(), KnowledgeDoc.created_at.desc()).first())
    chunks = chunk_text(body)
    sections = section_texts(chunks)
    vectors: list[list[float]] | None = None
    if embeddings.available():
        # the aliases are embedded with the passage so Hinglish phrasings land on it
        vectors = embeddings.embed([f"{c[1]} {c[2]} {c.translations.get('hinglish', '')}".strip() for c in chunks])
    status = "approved" if approved else "draft"
    doc = KnowledgeDoc(id=gen_uuid(), merchant_id=m, title=title, source=source, status=status,
                       approved_at=clock.now() if approved else None, content_hash=digest,
                       version=(previous.version + 1) if previous else 1,
                       supersedes_id=previous.id if previous else None)
    db.add(doc)
    db.flush()
    for i, c in enumerate(chunks):
        h, t, a = c
        db.add(KnowledgeChunk(doc_id=doc.id, merchant_id=m, position=i, heading=h, text=t, aliases=a or None,
                              translations=json.dumps(c.translations, ensure_ascii=False) if c.translations else None,
                              status=status, section_text=sections[i],
                              embedding=vectors[i] if vectors else None,
                              embedding_model=settings.embedding_model if vectors else None))
    if approved:
        _supersede_older(db, doc)
    db.commit()
    write_audit_entry(db, "knowledge_doc_added", {"doc_id": doc.id, "title": doc.title, "chunks": len(chunks),
                                                  "embedded": vectors is not None, "source": source,
                                                  "version": doc.version})
    return doc


def set_status(db: Session, doc_id: str, status: str) -> KnowledgeDoc:
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.id == doc_id).first()
    if doc is None:
        raise HTTPException(404, "Unknown document")
    doc.status = status
    doc.approved_at = clock.now() if status == "approved" else None
    db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == doc.id).update({"status": status})
    if status == "approved":
        _supersede_older(db, doc)           # approving an older version again rolls back to it
    db.commit()
    write_audit_entry(db, "knowledge_doc_approved" if status == "approved" else "knowledge_doc_unapproved",
                      {"doc_id": doc.id, "title": doc.title, "version": doc.version})
    return doc


# ── Re-embedding (a changed EMBEDDING_MODEL must not leave old vectors mixed with new ones) ──────────

_REEMBED_TABLES = (   # (model, text of the row to embed)
    (KnowledgeChunk, lambda r: f"{r.text} {r.aliases or ''} {(json.loads(r.translations).get('hinglish', '') if r.translations else '')}".strip()),
    (EpisodeChunk, lambda r: r.text),
    (CustomerFact, lambda r: f"{r.key.replace('_', ' ')}: {r.value}"),
)


def stale_vector_counts(db: Session) -> dict:
    """Rows whose vector is missing or was made by a different model than the current one."""
    return {M.__tablename__: db.query(M).filter(
        (M.embedding.is_(None)) | (M.embedding_model.is_(None)) | (M.embedding_model != settings.embedding_model)).count()
        for M, _ in _REEMBED_TABLES}


def reembed_stale(db: Session, batch: int = 64, limit: int = 2000) -> dict:
    """Re-embed rows made by another model (or never embedded). Refuses a vector of the wrong length: that
    needs a new EMBEDDING_DIM and rebuilt HNSW indexes, and mixing sizes would break search."""
    if not embeddings.available():
        return {"skipped": "embeddings_unavailable"}
    done: dict[str, int] = {}
    left = limit
    for M, render in _REEMBED_TABLES:
        n = 0
        while left > 0:
            rows = (db.query(M).filter((M.embedding.is_(None)) | (M.embedding_model.is_(None))
                                       | (M.embedding_model != settings.embedding_model))
                    .limit(min(batch, left)).all())
            if not rows:
                break
            vecs = embeddings.embed([render(r) for r in rows])
            if any(len(v) != settings.embedding_dim for v in vecs):
                db.rollback()
                write_audit_entry(db, "reembed_refused", {"table": M.__tablename__, "reason": "dimension_mismatch",
                                                          "expected": settings.embedding_dim,
                                                          "got": len(vecs[0]) if vecs else 0})
                return {**done, "refused": "dimension_mismatch", "table": M.__tablename__}
            for r, v in zip(rows, vecs):
                r.embedding, r.embedding_model = v, settings.embedding_model
            db.commit()
            n += len(rows)
            left -= len(rows)
        if n:
            done[M.__tablename__] = n
    if done:
        write_audit_entry(db, "embeddings_refreshed", {**done, "model": settings.embedding_model})
    return done


def delete_document(db: Session, doc_id: str) -> None:
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.id == doc_id).first()
    if doc is None:
        raise HTTPException(404, "Unknown document")
    db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == doc.id).delete()
    db.delete(doc)
    db.commit()
    write_audit_entry(db, "knowledge_doc_deleted", {"doc_id": doc_id, "title": doc.title})


def _doc_out(db: Session, d: KnowledgeDoc) -> dict:
    n = db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == d.id).count()
    return {"id": d.id, "title": d.title, "source": d.source, "status": d.status, "chunks": n,
            "created_at": d.created_at, "approved_at": d.approved_at, "version": d.version,
            "supersedes_id": d.supersedes_id}


# ── API ──────────────────────────────────────────────────────────────────────

class DocIn(BaseModel):
    title: str
    text: str
    approved: bool = False


class SearchIn(BaseModel):
    query: str


@router.get("/status")
def status(db: Session = Depends(get_db)):
    return {
        "embeddings": embeddings.available(), "embedding_model": settings.embedding_model,
        "vector_engine": "pgvector (HNSW)" if vectorstore.pgvector_active() else "in-process",
        "min_score": settings.retrieval_min_score,
        "docs": db.query(KnowledgeDoc).count(),
        "approved_passages": db.query(KnowledgeChunk).filter(KnowledgeChunk.status == "approved").count(),
        "stale_vectors": stale_vector_counts(db),
    }


@router.post("/reembed")
def reembed(db: Session = Depends(get_db)):
    """Re-embed every vector made by a different embedding model than the current one."""
    return reembed_stale(db)


@router.get("/docs")
def list_docs(db: Session = Depends(get_db)):
    docs = db.query(KnowledgeDoc).order_by(KnowledgeDoc.created_at.desc()).all()
    return [_doc_out(db, d) for d in docs]


@router.get("/docs/{doc_id}")
def get_doc(doc_id: str, db: Session = Depends(get_db)):
    d = db.query(KnowledgeDoc).filter(KnowledgeDoc.id == doc_id).first()
    if d is None:
        raise HTTPException(404, "Unknown document")
    chunks = db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == d.id).order_by(KnowledgeChunk.position).all()
    return {**_doc_out(db, d), "passages": [{"id": c.id, "heading": c.heading, "text": c.text,
                                              "section": c.section_text} for c in chunks]}


@router.post("/docs")
def create_doc(req: DocIn, db: Session = Depends(get_db)):
    return _doc_out(db, add_document(db, req.title, req.text, "pasted", approved=req.approved))


@router.post("/docs/upload")
async def upload_doc(request: Request, filename: str = "upload.txt", title: str | None = None,
                     db: Session = Depends(get_db)):
    """Raw file body (no multipart): .txt, .md or .pdf."""
    data = await request.body()
    body = extract_text(filename, data)
    return _doc_out(db, add_document(db, title or filename, body, f"file:{filename}"))


@router.post("/docs/{doc_id}/approve")
def approve(doc_id: str, db: Session = Depends(get_db)):
    return _doc_out(db, set_status(db, doc_id, "approved"))


@router.post("/docs/{doc_id}/unapprove")
def unapprove(doc_id: str, db: Session = Depends(get_db)):
    return _doc_out(db, set_status(db, doc_id, "draft"))


@router.delete("/docs/{doc_id}")
def remove(doc_id: str, db: Session = Depends(get_db)):
    delete_document(db, doc_id)
    return {"deleted": doc_id}


@router.post("/sample")
def load_sample(db: Session = Depends(get_db)):
    """Load the demo store policy (as a draft - approve it to let the agent use it)."""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "sample_policy.md"
    if db.query(KnowledgeDoc).filter(KnowledgeDoc.source == "sample").count():
        raise HTTPException(409, "The sample policy is already loaded.")
    doc = add_document(db, "Sample store policy (demo)", p.read_text(encoding="utf-8"), "sample")
    return _doc_out(db, doc)


@router.post("/search")
def try_search(req: SearchIn, db: Session = Depends(get_db)):
    """What the agent would find for this question (approved passages only)."""
    t0 = time.perf_counter()
    hits = retrieval.search_knowledge(db, req.query)
    ms = int((time.perf_counter() - t0) * 1000)
    return {"ms": ms, "min_score": settings.retrieval_min_score,
            "answerable": bool(hits and retrieval.is_answerable(hits[0])),
            "hits": [{"id": h.id, "title": h.title, "heading": h.extra.get("heading"), "text": h.text,
                      "score": round(h.score, 3)} for h in hits]}

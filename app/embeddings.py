"""
Local text embeddings (no API key, no per-call cost).

A small multilingual model (fastembed / ONNX, 384 dimensions) turns a sentence
into a vector so that "late fee kya hai" lands near a passage about late fees
even though the spelling differs. It runs on CPU: loading takes a few seconds
and the first run downloads the model once, so it is loaded lazily and warmed
up in a background thread at startup; query vectors are cached because the same
questions repeat.

If the model cannot load (offline first run, missing package) `available()` is
False and retrieval degrades to full-text search only - the call keeps working.
"""
import logging
import threading
from functools import lru_cache

from app.config import settings

logger = logging.getLogger(__name__)

_model = None
_failed = False
_lock = threading.Lock()


def _load():
    global _model, _failed
    with _lock:
        if _model is not None or _failed:
            return _model
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=settings.embedding_model)
            logger.info("embedding model ready: %s", settings.embedding_model)
        except Exception as e:           # missing package, offline first run, bad name
            _failed = True
            logger.warning("embedding model unavailable (%s); retrieval will use full-text only", type(e).__name__)
        return _model


def available() -> bool:
    return settings.rag_enabled and _load() is not None


def warm_up() -> None:
    """Load the model in the background so the first question is not slow."""
    if settings.rag_enabled:
        threading.Thread(target=_load, daemon=True, name="embedding-warmup").start()


def _normalise(v) -> list[float]:
    n = float(sum(float(x) * float(x) for x in v)) ** 0.5 or 1.0
    return [float(x) / n for x in v]


def embed(texts: list[str]) -> list[list[float]]:
    """Unit-length vectors (so cosine == dot product)."""
    m = _load()
    if m is None:
        raise RuntimeError("embedding model unavailable")
    return [_normalise(v) for v in m.embed([t or " " for t in texts])]


@lru_cache(maxsize=512)
def _embed_query_cached(text: str) -> tuple[float, ...]:
    return tuple(embed([text])[0])


def embed_query(text: str) -> list[float]:
    return list(_embed_query_cached((text or "").strip().lower()))

"""
How does retrieval behave as the data grows? (latency / scalability evidence)

  python scripts/bench_retrieval.py                 # 50,000 vectors, 20 tenants, 100 queries
  python scripts/bench_retrieval.py --n 200000      # bigger (needs time and a few GB of disk)

Builds a throw-away table of random unit vectors spread across tenants, then times the SAME
search function the call agent uses (app.vectorstore.vector_search, filtered to one tenant) under:
  - in-process   brute force over that tenant's rows (what the app does with no extension)
  - pgvector     sequential scan, then with the HNSW index (needs the extension: brew install pgvector)
and prints p50 / p95 per query. Also times the embedding model on a Hinglish sentence.
Uses DATABASE_URL (run it against the demo DB, not production). The table is dropped afterwards.
"""
import argparse
import io
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import embeddings, vectorstore            # noqa: E402
from app.config import settings                      # noqa: E402
from app.database import SessionLocal, engine        # noqa: E402

TABLE = "bench_vectors"


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def populate(n: int, tenants: int, dim: int) -> None:
    rng = np.random.default_rng(7)
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(f"CREATE TABLE {TABLE} (id serial PRIMARY KEY, merchant_id text NOT NULL, embedding real[] NOT NULL, text text)")
        done, t0 = 0, time.time()
        while done < n:
            m = min(5000, n - done)
            v = rng.normal(size=(m, dim)).astype("float32")
            v /= np.linalg.norm(v, axis=1, keepdims=True)
            buf = io.StringIO()
            for row in v:
                buf.write(f"t{rng.integers(tenants)}\t{{{','.join(f'{x:.5f}' for x in row)}}}\tbench\n")
            buf.seek(0)
            cur.copy_expert(f"COPY {TABLE} (merchant_id, embedding, text) FROM STDIN", buf)
            done += m
            print(f"\r  loaded {done:,}/{n:,}", end="", flush=True)
        cur.execute(f"CREATE INDEX ON {TABLE} (merchant_id)")
        cur.execute(f"ANALYZE {TABLE}")
        raw.commit()
        print(f"  ({time.time() - t0:.0f}s)")
    finally:
        raw.close()


def timed(label: str, queries: np.ndarray, tenants: int, k: int = 4) -> tuple[float, float]:
    db = SessionLocal()
    ts = []
    try:
        for i, q in enumerate(queries):
            t0 = time.perf_counter()
            vectorstore.vector_search(db, TABLE, "merchant_id = :m", {"m": f"t{i % tenants}"}, q.tolist(), k)
            ts.append((time.perf_counter() - t0) * 1000)
            db.rollback()
    finally:
        db.close()
    p50, p95 = statistics.median(ts), pct(ts, 0.95)
    print(f"  {label:32} p50 {p50:7.1f} ms   p95 {p95:7.1f} ms   ({len(ts)} queries)")
    return p50, p95


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--tenants", type=int, default=20)
    ap.add_argument("--queries", type=int, default=100)
    a = ap.parse_args()
    dim = settings.embedding_dim
    print(f"Building {a.n:,} random {dim}-d vectors across {a.tenants} tenants ...")
    populate(a.n, a.tenants, dim)
    rng = np.random.default_rng(11)
    qs = rng.normal(size=(a.queries, dim)).astype("float32")
    qs /= np.linalg.norm(qs, axis=1, keepdims=True)

    print("\nSearch latency, filtered to ONE tenant (what a call does):")
    vectorstore._state["pgvector"] = False
    timed("in-process (no extension)", qs, a.tenants)
    has_pg = vectorstore.init(engine)       # tries CREATE EXTENSION; also builds the app's own indexes
    if has_pg:
        db = SessionLocal()
        db.execute(text(f"DROP INDEX IF EXISTS {TABLE}_hnsw")); db.commit(); db.close()
        timed("pgvector, sequential scan", qs, a.tenants)
        t0 = time.time()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f"CREATE INDEX {TABLE}_hnsw ON {TABLE} USING hnsw ((embedding::vector({dim})) vector_cosine_ops)"))
            c.execute(text(f"ANALYZE {TABLE}"))
        print(f"  (HNSW index built in {time.time() - t0:.0f}s)")
        timed("pgvector + HNSW index", qs, a.tenants)
    else:
        print("  pgvector is not installed here: `brew install pgvector`, then run this again to see the HNSW numbers.")

    print("\nEmbedding a question with the local model:")
    if embeddings.available():
        embeddings.embed_query("warm up")
        ts = []
        for i in range(30):
            t0 = time.perf_counter()
            embeddings._embed_query_cached.cache_clear()
            embeddings.embed_query(f"refund kitne din mein aata hai number {i}")
            ts.append((time.perf_counter() - t0) * 1000)
        print(f"  {settings.embedding_model.split('/')[-1]:32} p50 {statistics.median(ts):7.1f} ms   p95 {pct(ts, .95):7.1f} ms (uncached)")
    else:
        print("  embedding model not available")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    print("\nDone (benchmark table dropped).")


if __name__ == "__main__":
    main()

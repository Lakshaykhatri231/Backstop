"""Policy search: the right passage comes back for Hinglish / Hindi / English questions, and unrelated questions are rejected."""
import json

from evals.harness import ROOT, Recorder


def run(ctx):
    from app import embeddings, knowledge, retrieval, vectorstore
    from app.config import settings
    from app.database import SessionLocal
    R = Recorder("retrieval", "Hybrid (vector + keyword) search over the approved policy finds the right section and rejects questions it cannot answer.")
    if not embeddings.available():
        return R.skip("the local embedding model is not available (RAG_ENABLED off or the model failed to load)")
    db = SessionLocal()
    try:
        body = (ROOT / "data/store_policy.md").read_text(encoding="utf-8")
        knowledge.add_document(db, "Backstop Store policy", body, approved=True)
        aliases = {a.strip().lower() for c in knowledge.chunk_text(body) for a in c[2].split(";") if a.strip()}
        rows = [json.loads(l) for l in (ROOT / "evals/data/retrieval.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        floor = settings.retrieval_min_score
        n = r1 = r3 = acc = rr = 0.0
        un = un_ok = vec3 = fts3 = 0
        by_lang: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            # an eval question that is also an "Also asked as" alias would be marking its own homework
            R.case(f"no_leak:{i}", r["q"].strip().lower() not in aliases, "the question is an alias in the document", critical=True)
            hits = retrieval.search_knowledge(db, r["q"], k=4)
            top = hits[0].score if hits else 0.0
            if r.get("unanswerable"):
                un += 1
                ok = (not hits) or not retrieval.is_answerable(hits[0])
                un_ok += ok
                R.case(f"unanswerable:{r['q'][:40]}", ok, f"scored {top:.2f} (floor {floor}); top passage was {hits[0].extra.get('heading') if hits else None!r}")
                continue
            n += 1
            heads = [h.extra.get("heading") for h in hits]
            pos = heads.index(r["expect_heading"]) + 1 if r["expect_heading"] in heads else 0
            r1 += pos == 1
            r3 += 0 < pos <= 3
            rr += (1 / pos) if pos else 0
            acc += bool(hits) and retrieval.is_answerable(hits[0])
            lang = "hindi" if any("ऀ" <= ch <= "ॿ" for ch in r["q"]) else ("english" if r["q"].isascii() and not any(w in r["q"].lower().split() for w in ("kya", "hai", "mujhe", "aap", "main", "mera", "kitne", "kab", "kaise", "ko", "ho", "ka", "ki")) else "hinglish")
            by_lang.setdefault(lang, [0, 0])
            by_lang[lang][0] += 0 < pos <= 3
            by_lang[lang][1] += 1
            R.case(f"answerable:{r['q'][:40]}", 0 < pos <= 3, f"expected {r['expect_heading']!r}, got {heads[:3]} (top score {top:.2f})")
            # ablation: each half of the hybrid on its own
            where, params = "merchant_id = :m AND status = 'approved'", {"m": settings.default_merchant_id}
            vs = vectorstore.vector_search(db, "knowledge_chunks", where, params, embeddings.embed_query(r["q"]), 3)
            ks = vectorstore.fts_search(db, "knowledge_chunks", where, params, r["q"], 3,
                                        "text || ' ' || coalesce(aliases, '') || ' ' || coalesce(translations, '')")
            vheads = _headings(db, [i for i, _ in vs])
            kheads = _headings(db, [i for i, _ in ks])
            vec3 += r["expect_heading"] in vheads
            fts3 += r["expect_heading"] in kheads
        R.rate("recall_at_1", r1, n)
        R.rate("recall_at_3", r3, n)
        R.metric("mrr", rr / n)
        R.rate("answerable_accepted_rate", acc, n)
        R.rate("unanswerable_rejected_rate", un_ok, un)
        R.rate("ablation_vector_only_recall_at_3", vec3, n)
        R.rate("ablation_keyword_only_recall_at_3", fts3, n)
        for lang, (ok, total) in by_lang.items():
            R.rate(f"recall_at_3_{lang}", ok, total)
        # Bars below are calibrated from the first baseline (recall@1 0.66, recall@3 0.88, accept 0.84, reject 0.92) with a little room.
        R.require("recall_at_3", ">=", 0.85)
        R.require("recall_at_1", ">=", 0.60)
        R.require("answerable_accepted_rate", ">=", 0.80, "questions the policy covers must be judged answerable")
        R.require("unanswerable_rejected_rate", ">=", 0.85, "questions it cannot answer must not be answered from a random passage")
        R.require("recall_at_3", ">=", R.result.metrics["ablation_keyword_only_recall_at_3"], "hybrid must not be worse than keyword search alone")
        return R.result
    finally:
        db.close()


def _headings(db, ids: list[str]) -> list[str]:
    from sqlalchemy import text
    if not ids:
        return []
    rows = db.execute(text("SELECT id, heading FROM knowledge_chunks WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}).all()
    by = {str(r[0]): r[1] for r in rows}
    return [by[i] for i in ids if i in by]

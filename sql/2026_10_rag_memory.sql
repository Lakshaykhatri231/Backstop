-- Live-call agent: persona, hands-free call, merchant knowledge (RAG) and customer memory.
-- Idempotent; safe to run before or after the first start of the new code. New TABLES come
-- from Base.metadata.create_all; this file only adds COLUMNS to tables that already exist
-- (create_all never alters) and the optional pgvector extension.

-- Phase 1: voice_turns gained timings / interruption / policy sources.
ALTER TABLE IF EXISTS voice_turns ADD COLUMN IF NOT EXISTS timings_json TEXT;
ALTER TABLE IF EXISTS voice_turns ADD COLUMN IF NOT EXISTS interrupted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS voice_turns ADD COLUMN IF NOT EXISTS sources TEXT;

-- Phase 5: separate, customer-ticked consent for remembering notes between calls.
ALTER TABLE IF EXISTS contact_points ADD COLUMN IF NOT EXISTS memory_consent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS contact_points ADD COLUMN IF NOT EXISTS memory_consent_at TIMESTAMP;

-- Tiered memory: which wording of the memory consent the customer agreed to (v2 = also keeps a record of each
-- conversation). Existing consents stay NULL = v1 (notes only) until the customer ticks the new wording.
ALTER TABLE IF EXISTS contact_points ADD COLUMN IF NOT EXISTS memory_consent_version VARCHAR;

-- Knowledge store: which embedding model made each vector, parent section text, document versions + dedupe hash.
ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_model VARCHAR;
ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS section_text TEXT;
ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS translations TEXT;
ALTER TABLE IF EXISTS knowledge_docs ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE IF EXISTS knowledge_docs ADD COLUMN IF NOT EXISTS supersedes_id UUID;
ALTER TABLE IF EXISTS knowledge_docs ADD COLUMN IF NOT EXISTS content_hash VARCHAR;
CREATE INDEX IF NOT EXISTS ix_knowledge_docs_content_hash ON knowledge_docs (content_hash);

-- New tables (conversation_episodes, episode_chunks, customer_facts, memory_links) come from create_all.

-- Phase 3/4 (optional, speeds up search at scale). The app also works WITHOUT this extension
-- (it searches in process); on start it tries to create the extension and these indexes itself.
-- CREATE EXTENSION IF NOT EXISTS vector;
-- CREATE INDEX IF NOT EXISTS knowledge_chunks_hnsw ON knowledge_chunks USING hnsw ((embedding::vector(384)) vector_cosine_ops);
-- CREATE INDEX IF NOT EXISTS customer_memory_hnsw ON customer_memory USING hnsw ((embedding::vector(384)) vector_cosine_ops);
-- CREATE INDEX IF NOT EXISTS episode_chunks_hnsw ON episode_chunks USING hnsw ((embedding::vector(384)) vector_cosine_ops);
-- CREATE INDEX IF NOT EXISTS customer_facts_hnsw ON customer_facts USING hnsw ((embedding::vector(384)) vector_cosine_ops);

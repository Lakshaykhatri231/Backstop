"""
Tables behind the two retrieval stores. Kept apart on purpose:

- knowledge_*    : what the MERCHANT says (policies, how-tos). Shared by all of that
                   merchant's customers, written by the merchant, spoken as fact, only
                   when `approved`.
- customer_memory: what we noted about ONE customer (v1 notes). Private to that contact,
                   written by the system only with the customer's consent, advisory,
                   expires, and deletable on request.
- conversation_episodes / episode_chunks / customer_facts / memory_links: the tiered memory
                   (episodic summary, the conversation word for word, durable facts, and how
                   they relate). Same rules as customer_memory - one contact, v2 consent,
                   expiring, deleted together - see memory.py / memory_write.py.

Embeddings are stored as REAL[] (portable - the app works with no extension). When the
pgvector extension is present, search casts them (`embedding::vector(384)`) and uses an
HNSW expression index; otherwise vectors are compared in process. `merchant_id` is on
every row: the app is single-merchant today, but tenancy is then a WHERE clause.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, REAL, UUID

from app.database import Base
from app.models import gen_uuid


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    title = Column(String, nullable=False)
    source = Column(String, nullable=False, default="pasted")        # pasted | file:<name> | sample
    status = Column(String, nullable=False, default="draft")         # draft | approved
    language = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)               # re-adding the same title bumps this
    supersedes_id = Column(UUID(as_uuid=False), nullable=True)         # the older version this one replaced
    content_hash = Column(String, nullable=True, index=True)           # sha256 of the body: identical uploads are refused


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    doc_id = Column(UUID(as_uuid=False), ForeignKey("knowledge_docs.id", ondelete="CASCADE"), nullable=False, index=True)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    position = Column(Integer, nullable=False, default=0)
    heading = Column(String, nullable=True)
    text = Column(Text, nullable=False)            # what is spoken
    aliases = Column(Text, nullable=True)          # "also asked as" phrasings (Hinglish etc.): searched, never spoken
    status = Column(String, nullable=False, default="draft", index=True)   # mirrors the doc: only 'approved' is searchable
    embedding = Column(ARRAY(REAL), nullable=True)
    embedding_model = Column(String, nullable=True)   # which model made `embedding` (re-embed job compares to settings)
    section_text = Column(Text, nullable=True)        # the whole section this passage came from (parent text)
    translations = Column(Text, nullable=True)        # JSON {"hinglish": ..., "hi": ...}: the passage in the call's language


class CustomerMemory(Base):
    __tablename__ = "customer_memory"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    contact_id = Column(UUID(as_uuid=False), nullable=False, index=True)     # ContactPoint.id
    kind = Column(String, nullable=False)       # preference | stated_constraint | callback_time | hardship_flag | objection
    text = Column(String, nullable=False)       # <= 140 chars, scrubbed
    source_call_id = Column(String, nullable=True)
    embedding = Column(ARRAY(REAL), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)


# ── Tiered conversation memory (one contact each; all gated on the v2 memory consent) ────────────

class ConversationEpisode(Base):
    """One finished call, as an event: what happened, how it went. Summary fields are written by the
    background step and are scrubbed; the words themselves live in EpisodeChunk (word for word)."""
    __tablename__ = "conversation_episodes"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    contact_id = Column(UUID(as_uuid=False), nullable=False, index=True)       # ContactPoint.id
    call_id = Column(String, nullable=False, unique=True)
    subject_type = Column(String, nullable=True)
    subject_id = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=False)
    outcome = Column(String, nullable=True)                 # from VoiceCall.routed_to
    summary = Column(Text, nullable=True)                   # 2-3 sentences, scrubbed (null until the slow step)
    tone_arc = Column(String, nullable=True)                # e.g. "tense -> relieved"
    key_events = Column(Text, nullable=True)                # JSON list of short strings
    importance = Column(Float, nullable=False, default=0.5)  # 0..1
    summarised_at = Column(DateTime, nullable=True)
    summary_source = Column(String, nullable=True)          # llm | rules
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)


class EpisodeChunk(Base):
    """The conversation word for word, in 4-turn windows ("Agent: ...\nCustomer: ..."). Verbatim on
    purpose: it is a copy of what was said, so it is NOT scrubbed (the summary and facts are)."""
    __tablename__ = "episode_chunks"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    contact_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    episode_id = Column(UUID(as_uuid=False), ForeignKey("conversation_episodes.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    seq = Column(Integer, nullable=False, default=0)
    turn_from = Column(Integer, nullable=False)
    turn_to = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    embedding = Column(ARRAY(REAL), nullable=True)
    embedding_model = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)


class CustomerFact(Base):
    """A durable thing we know about the person (salary day, best time to call...). One ACTIVE row per
    key; a changed value supersedes the old row (kept, with valid_to) instead of overwriting it."""
    __tablename__ = "customer_facts"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    contact_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    key = Column(String, nullable=False)                    # memory_write.FACT_KEYS
    value = Column(String, nullable=False)                  # <= 140 chars, scrubbed
    confidence = Column(Float, nullable=False, default=0.7)
    importance = Column(Float, nullable=False, default=0.5)
    status = Column(String, nullable=False, default="active", index=True)   # active | superseded
    valid_from = Column(DateTime, nullable=False, default=datetime.utcnow)
    valid_to = Column(DateTime, nullable=True)
    last_confirmed = Column(DateTime, nullable=False, default=datetime.utcnow)
    source_episode_id = Column(UUID(as_uuid=False), nullable=True)
    embedding = Column(ARRAY(REAL), nullable=True)
    embedding_model = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)


class MemoryLink(Base):
    """A typed edge between two memory items of one contact: temporal_next | entity | causal | supersedes."""
    __tablename__ = "memory_links"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(String, nullable=False, default="default", index=True)
    contact_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    src_type = Column(String, nullable=False)               # episode | fact | promise | subject
    src_id = Column(String, nullable=False, index=True)
    dst_type = Column(String, nullable=False)
    dst_id = Column(String, nullable=False, index=True)
    relation = Column(String, nullable=False)
    weight = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=datetime.utcnow)

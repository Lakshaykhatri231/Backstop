"""
Tiered call memory - the READ side. What the assistant is allowed to remember at the start of a turn.

Only for a customer on the current (v2) memory wording; everyone else gets the older note lookup
(`retrieval.search_memory`) or nothing. Every query is scoped to ONE contact in the SQL itself.

Because the model's reading of the line (its intent) comes out of the same single call that writes the
reply, retrieval cannot wait for it. So the reads are tiered by cost:

- ALWAYS (cheap, structured, no vector search): the CORE CARD - the person's active facts plus a one-line
  summary of the last call. This is also how hardship / tone carry over to the next call.
- ON A CUE (the customer's words ask about the past: "pichli baar", "last time", "maine kaha tha"): the last
  call's summary and the best-matching WORD-FOR-WORD windows of past calls, found with the same hybrid
  vector + keyword search (reciprocal-rank fusion) the policy store uses, scored by
  relevance x recency x importance x confidence, plus one hop of links.
- If the model itself calls the line a memory question (Q_MEMORY) and nothing was fetched, the caller
  asks for a forced recall - the same second pass the policy path already uses.

Everything returned carries an id and a human date ("your call on 12 Oct") so a reply can cite where it
came from, and the whole block is capped at `memory_budget_chars`.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, retrieval
from app.config import settings

# Wording that asks about the past conversation (Roman Hinglish, English, Devanagari).
_RECALL_CUE = re.compile(
    r"pichl[ei]\s*(baar|call|bar)|pichli\s*baar|pehle\s*(call|baat|wali)|last\s*time|previous(ly)?\s*(call|time|conversation)|"
    r"earlier\s*(call|conversation)|we\s*(talked|spoke|discussed)|you\s*(said|told|promised)|i\s*(said|told|had said)|"
    r"maine\s*(kaha|bola|boli|bataya|batayi|bataayi|bataai)|aapne\s*(kaha|bola|bataya)|tumne\s*(kaha|bola)|kya\s*baat\s*hui|baat\s*hui\s*thi|"
    r"yaad\s*(hai|nahi)|remember|(date|tareekh|din)\s*(kaunsi|konsi|kaun\s*si)?.{0,20}(bataayi|batayi|bataya|boli|bola)\s*(thi|tha)|"
    r"पिछली\s*बार|पिछले\s*कॉल|पहले\s*की\s*बात|मैंने\s*(कहा|बोला|बताया)|आपने\s*(कहा|बोला|बताया)|याद", re.IGNORECASE)

MAX_CORE_FACTS = 5
RELEVANCE_FLOOR = 0.15          # below this a window is not "about" the question


def has_recall_cue(text_: str) -> bool:
    return bool(_RECALL_CUE.search(text_ or ""))


# ── Pure scoring / budget helpers (unit-tested) ──────────────────────────────

def recency_decay(age_days: float, half_life_days: float | None = None) -> float:
    half = float(half_life_days or settings.memory_half_life_days)
    return 0.5 ** (max(0.0, age_days) / max(1.0, half))


def score_item(relevance: float, age_days: float, importance: float, confidence: float = 1.0,
               half_life_days: float | None = None) -> float:
    """relevance x recency decay x (0.5 + importance) x confidence. Importance and confidence are 0..1."""
    return (relevance * recency_decay(age_days, half_life_days)
            * (0.5 + max(0.0, min(1.0, importance))) * max(0.0, min(1.0, confidence)))


def relevance_of(cosine: float | None, rrf: float) -> float:
    """Cosine when the vector search found it; otherwise a keyword-only hit, scaled from its RRF rank
    (a rank-1 keyword hit has rrf ~ 1/61, so x30 brings it near 0.5)."""
    return float(cosine) if cosine is not None else min(1.0, rrf * 30.0)


def fit_budget(lines: list[str], budget: int | None = None) -> list[str]:
    """Keep lines, in order, until the character budget is spent (a line that does not fit is dropped,
    never cut, so a quoted sentence is never half-quoted)."""
    left = budget if budget is not None else settings.memory_budget_chars
    out = []
    for ln in lines:
        if len(ln) + 1 > left:
            continue
        out.append(ln)
        left -= len(ln) + 1
    return out


def when(dt: datetime | None, now: datetime | None = None) -> str:
    """"12 Oct" - how a past call is named aloud."""
    return dt.strftime("%d %b").lstrip("0") if dt else "an earlier day"


# ── What comes back ──────────────────────────────────────────────────────────

@dataclass
class Memory:
    """Lines for the model, tagged so a reply can cite them, plus a trace of what was read."""
    core: list[str] = field(default_factory=list)          # always-on profile lines
    recalled: list[dict] = field(default_factory=list)     # {mid, kind: summary|window, id, episode_id, when, text, score}
    trace: dict = field(default_factory=dict)

    @property
    def recalled_ids(self) -> set[str]:
        return {r["mid"] for r in self.recalled}

    @property
    def core_ids(self) -> set[str]:
        return {f"c{i}" for i in range(1, len(self.core) + 1)}

    @property
    def citable_ids(self) -> set[str]:
        """What a Q_MEMORY reply may cite: the profile lines (c1..) and the recalled items (m1..)."""
        return self.core_ids | self.recalled_ids

    def prompt_lines(self) -> list[str]:
        lines = [f"[c{i}] {c}" for i, c in enumerate(self.core, 1)]
        for r in self.recalled:
            what = "summary of your call on" if r["kind"] == "summary" else "word for word, your call on"
            lines.append(f"[{r['mid']}] ({what} {r['when']}) {r['text']}")
        return fit_budget(lines)

    def known_text(self) -> str:
        # the call dates are part of what the model is told to say ("on your call of 12 Oct"), so they are known too
        return " ".join([*self.core, *[f"{r['when']} {r['text']}" for r in self.recalled]])

    def empty(self) -> bool:
        return not self.core and not self.recalled


# ── Reads ────────────────────────────────────────────────────────────────────

_FACT_LABEL = {"salary_day": "money usually arrives", "preferred_call_time": "best time to call",
               "preferred_language": "language", "hardship_flag": "gentle handling",
               "objection_dispute": "disputed before", "commitment_style": "how they commit",
               "preference": "preference", "stated_constraint": "constraint"}


def core_card(db: Session, contact_id: str, now: datetime | None = None) -> tuple[list[str], dict]:
    """(lines, trace). Active facts, most important first, then the last call in one line."""
    now = now or clock.now()
    facts = db.execute(text(
        "SELECT id, key, value FROM customer_facts WHERE contact_id = CAST(:c AS uuid) AND merchant_id = :m "
        "AND status = 'active' AND expires_at > :now ORDER BY importance DESC, last_confirmed DESC LIMIT :n"),
        {"c": contact_id, "m": settings.default_merchant_id, "now": now, "n": MAX_CORE_FACTS}).all()
    lines = [f"{_FACT_LABEL.get(r[1], r[1])}: {r[2]}" for r in facts]
    last = db.execute(text(
        "SELECT id, started_at, summary, tone_arc FROM conversation_episodes WHERE contact_id = CAST(:c AS uuid) "
        "AND merchant_id = :m AND expires_at > :now AND summary IS NOT NULL ORDER BY started_at DESC LIMIT 1"),
        {"c": contact_id, "m": settings.default_merchant_id, "now": now}).first()
    if last is not None:
        tone = f" (mood: {last[3]})" if last[3] else ""
        lines.append(f"last call, {when(last[1])}{tone}: {last[2]}")
    return lines, {"facts": [str(r[0]) for r in facts], "last_episode": str(last[0]) if last else None}


def _episode_rows(db: Session, ids: list[str], now: datetime | None = None) -> dict:
    """Episodes by id - never an expired one: retention holds from the moment a call expires, not from the next sweep."""
    if not ids:
        return {}
    rows = db.execute(text(
        "SELECT id, started_at, importance, summary FROM conversation_episodes "
        "WHERE id = ANY(CAST(:ids AS uuid[])) AND expires_at > :now"),
        {"ids": ids, "now": now or clock.now()}).all()
    return {str(r[0]): r for r in rows}


def recall(db: Session, contact_id: str, query: str, now: datetime | None = None, k: int | None = None) -> list[dict]:
    """The last call's summary + the best word-for-word windows for `query`, best first, with one hop of links."""
    now = now or clock.now()
    k = k or settings.memory_top_k
    base = {"c": contact_id, "m": settings.default_merchant_id}
    out: list[dict] = []

    # 1. the last call's summary - "last time" is a question about recency, not about similarity
    last = db.execute(text(
        "SELECT id, started_at, summary FROM conversation_episodes WHERE contact_id = CAST(:c AS uuid) AND merchant_id = :m "
        "AND expires_at > :now AND summary IS NOT NULL ORDER BY started_at DESC LIMIT 1"), {**base, "now": now}).first()
    seen_episodes: set[str] = set()
    if last is not None:
        seen_episodes.add(str(last[0]))
        out.append({"kind": "summary", "id": str(last[0]), "episode_id": str(last[0]), "when": when(last[1]),
                    "text": last[2], "score": 1.0})

    # 2. best word-for-word windows (vector + keyword, fused), scored with recency x importance
    where = "merchant_id = :m AND contact_id = CAST(:c AS uuid) AND expires_at > :now"
    found = retrieval._hybrid(db, "episode_chunks", where, {**base, "now": now}, query, k * 2)
    if found:
        rows = db.execute(text(
            "SELECT id, episode_id, text FROM episode_chunks WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [i for i, _, _ in found]}).all()
        chunk = {str(r[0]): r for r in rows}
        eps = _episode_rows(db, sorted({str(r[1]) for r in rows}), now)
        scored = []
        for cid, cos, rrf in found:
            r = chunk.get(cid)
            ep = eps.get(str(r[1])) if r else None
            if r is None or ep is None:
                continue
            rel = relevance_of(cos, rrf)
            if rel < RELEVANCE_FLOOR:
                continue
            age = max(0.0, (now - ep[1]).total_seconds() / 86400.0)
            scored.append((score_item(rel, age, ep[2] or 0.5), cid, r, ep))
        scored.sort(key=lambda s: -s[0])
        for sc, cid, r, ep in scored[:k]:
            out.append({"kind": "window", "id": cid, "episode_id": str(r[1]), "when": when(ep[1]),
                        "text": r[2], "score": round(sc, 3)})
            seen_episodes.add(str(r[1]))

    # 3. one hop of links: the summary of the call just before / after / causally tied to a matched call
    if seen_episodes:
        linked = db.execute(text(
            "SELECT DISTINCT CASE WHEN src_id = ANY(:ids) THEN dst_id ELSE src_id END FROM memory_links "
            "WHERE contact_id = CAST(:c AS uuid) AND relation IN ('temporal_next', 'causal') "
            "AND src_type = 'episode' AND dst_type = 'episode' AND (src_id = ANY(:ids) OR dst_id = ANY(:ids))"),
            {"c": contact_id, "ids": sorted(seen_episodes)}).all()
        extra_ids = [r[0] for r in linked if r[0] not in seen_episodes][:1]
        for eid, ep in _episode_rows(db, extra_ids, now).items():
            if ep[3]:
                out.append({"kind": "summary", "id": eid, "episode_id": eid, "when": when(ep[1]),
                            "text": ep[3], "score": 0.0})
    return out


def build(db: Session, cp, text_: str, now: datetime | None = None, force_recall: bool = False) -> Memory:
    """The memory for this turn. Empty (and free) unless the customer agreed to the current wording."""
    from app import contacts
    mem = Memory()
    if cp is None or not settings.rag_enabled or not contacts.has_full_memory_consent(cp):
        return mem
    now = now or clock.now()
    mem.core, core_trace = core_card(db, cp.id, now)
    cue = has_recall_cue(text_) or force_recall
    if cue:
        for n, item in enumerate(recall(db, cp.id, text_, now), 1):
            mem.recalled.append({**item, "mid": f"m{n}"})
    # the last call's one-line summary is already in the profile; do not spend budget on it twice
    if any(r["kind"] == "summary" and r["id"] == core_trace.get("last_episode") for r in mem.recalled):
        mem.core = [c for c in mem.core if not c.startswith("last call,")]
    mem.trace = {"core": core_trace, "cue": cue,
                 "recalled": [{"mid": r["mid"], "kind": r["kind"], "id": r["id"], "episode_id": r["episode_id"],
                               "score": r["score"]} for r in mem.recalled]}
    return mem

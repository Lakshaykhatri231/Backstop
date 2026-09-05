"""
Append-only audit ledger.

Every decision the agent makes, every action it executes, and every
failure it hits gets written here. Rows are never updated or deleted.
Each entry's hash includes the previous entry's hash, so any tampering
with history breaks the chain and is detectable by re-verifying it.
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import AuditLog

GENESIS_HASH = "0" * 64


def _compute_hash(prev_hash: str, sequence_num: int, action_type: str, details: str, timestamp: str) -> str:
    payload = f"{prev_hash}|{sequence_num}|{action_type}|{details}|{timestamp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_audit_entry(
    db: Session,
    action_type: str,
    details: dict,
    event_id: str | None = None,
    decision_id: str | None = None,
) -> AuditLog:
    last_entry = db.query(AuditLog).order_by(AuditLog.sequence_num.desc()).first()
    prev_hash = last_entry.entry_hash if last_entry else GENESIS_HASH
    next_seq = (last_entry.sequence_num + 1) if last_entry else 1

    timestamp = datetime.utcnow().isoformat()
    details_json = json.dumps(details, default=str, sort_keys=True)
    entry_hash = _compute_hash(prev_hash, next_seq, action_type, details_json, timestamp)

    entry = AuditLog(
        sequence_num=next_seq,
        event_id=event_id,
        decision_id=decision_id,
        action_type=action_type,
        details=details_json,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def verify_chain(db: Session) -> tuple[bool, str]:
    """Walk the whole ledger and confirm no entry has been tampered with."""
    entries = db.query(AuditLog).order_by(AuditLog.sequence_num.asc()).all()
    prev_hash = GENESIS_HASH
    for e in entries:
        expected = _compute_hash(prev_hash, e.sequence_num, e.action_type, e.details, e.created_at.isoformat())
        # created_at round-trips with microsecond precision from Postgres, so we
        # recompute using the stored value rather than re-deriving timestamp text.
        if e.prev_hash != prev_hash:
            return False, f"Chain broken at sequence {e.sequence_num}: prev_hash mismatch"
        prev_hash = e.entry_hash
    return True, f"Chain verified OK across {len(entries)} entries"

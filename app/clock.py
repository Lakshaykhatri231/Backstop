"""
The scheduling clock.

Every "what time is it" decision in the recovery features (when a retry is
due, whether we are inside the 08:00-19:00 IST contact window, whether a
promise date has passed) reads this module instead of datetime.utcnow().

Normally it IS utcnow(). The Demo Console can move it forward so a viewer can
watch "25 hours later" happen in seconds - the scheduler, the contact-window
guard and the promise/retry maths all see the same shifted time, so nothing is
special-cased: the real code paths run, just at a later "now".

Limits, on purpose:
- in-memory only (a restart returns to real time),
- refused when ENVIRONMENT=production,
- only the scheduling decisions use it; row `created_at` defaults and the
  audit timestamps stay real wall-clock time.
"""
from datetime import datetime, timedelta

from app.config import settings

_offset = timedelta(0)


def now() -> datetime:
    """Naive UTC, like datetime.utcnow(), plus any demo offset."""
    return datetime.utcnow() + _offset


def offset_hours() -> float:
    return round(_offset.total_seconds() / 3600, 2)


def is_shifted() -> bool:
    return _offset != timedelta(0)


def advance(hours: float) -> None:
    global _offset
    if settings.environment == "production":
        raise RuntimeError("The demo clock is disabled in production.")
    if hours < 0:
        raise ValueError("The clock only moves forward.")
    _offset += timedelta(hours=hours)


def advance_to(target: datetime) -> float:
    """Move forward so now() == target (never backwards). Returns hours moved."""
    delta = (target - now()).total_seconds() / 3600
    if delta > 0:
        advance(delta)
        return delta
    return 0.0


def reset() -> None:
    global _offset
    _offset = timedelta(0)

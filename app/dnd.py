"""
Do-Not-Disturb (NCPR) check, run before every automated call.

In India a number can be registered as "do not disturb"; a business must not
call it (outside the narrow permitted categories) and a violation can be
penalised even when the customer ticked consent on the merchant's own site -
consent and DND are separate protections, so voice enforces both.

v1 is SIMULATED, like the voice and OTP providers: a configurable list of
numbers counts as registered, so the block path is testable. It FAILS CLOSED:
in production the simulated registry is refused (a fake "not on DND" must
never green-light a real call), and so is any unknown provider name.
A real implementation is one class with the same is_registered() contract;
whether a given call category (e.g. a transaction alert about the customer's
own payment) is exempt is a compliance call to be confirmed with someone who
knows the rule - this module only answers "is the number on the list".
"""
from typing import Protocol

from app.config import settings
from app.otp import OtpError, normalize_phone


class DndChecker(Protocol):
    def is_registered(self, phone: str) -> bool: ...


class SimulatedDndChecker:
    def is_registered(self, phone: str) -> bool:
        listed: set[str] = set()
        for raw in settings.dnd_simulated_numbers.split(","):
            if raw.strip():
                try:
                    listed.add(normalize_phone(raw))
                except OtpError:
                    continue
        return phone in listed


def check(phone: str | None) -> tuple[bool, str | None]:
    """(allowed, block_reason)."""
    if not phone:
        return False, "no_phone_number"
    if settings.dnd_provider != "simulated":
        return False, "dnd_provider_not_configured"
    if settings.environment == "production":
        return False, "dnd_provider_not_configured"
    if SimulatedDndChecker().is_registered(phone):
        return False, "dnd_registered"
    return True, None

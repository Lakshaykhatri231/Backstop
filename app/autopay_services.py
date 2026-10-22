"""
What a customer can start autopay for on their own page (app/accounts.py).

Three fixed demo services plus a custom amount. The merchant's catalogue is
hardcoded on purpose: this is the menu, not a product engine. Bounds on the
custom amount are hardcoded too - the page must never create a mandate for an
amount nobody agreed to.
"""
SERVICES = [
    {"key": "plus", "name": "Backstop Plus", "amount_inr": 499.0, "frequency": "monthly",
     "blurb": "Free delivery on every order"},
    {"key": "pro", "name": "Backstop Pro", "amount_inr": 999.0, "frequency": "monthly",
     "blurb": "Free delivery, early access and priority support"},
    {"key": "weekly_box", "name": "Weekly Box", "amount_inr": 149.0, "frequency": "weekly",
     "blurb": "A small box every week"},
]
CUSTOM_KEY = "custom"
CUSTOM_MIN_INR = 10.0
CUSTOM_MAX_INR = 5000.0
FREQUENCIES = ("weekly", "monthly", "yearly")


class ServiceError(ValueError):
    pass


def resolve(service_key: str, amount_inr: float | None = None, frequency: str | None = None) -> dict:
    """The (name, amount, frequency) to charge. A named service ignores any
    amount sent with it - the menu price wins."""
    for s in SERVICES:
        if s["key"] == service_key:
            return {"key": s["key"], "name": s["name"], "amount_inr": s["amount_inr"], "frequency": s["frequency"]}
    if service_key != CUSTOM_KEY:
        raise ServiceError("Unknown service")
    if amount_inr is None or not (CUSTOM_MIN_INR <= amount_inr <= CUSTOM_MAX_INR):
        raise ServiceError(f"A custom amount must be between Rs {CUSTOM_MIN_INR:.0f} and Rs {CUSTOM_MAX_INR:.0f}")
    if frequency not in FREQUENCIES:
        raise ServiceError("Pick weekly, monthly or yearly")
    return {"key": CUSTOM_KEY, "name": "Custom autopay", "amount_inr": round(float(amount_inr), 2),
            "frequency": frequency}

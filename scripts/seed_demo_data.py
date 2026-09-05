"""
Pre-seeds realistic demo data before showing the dashboard to judges.
Generates a mix of payment failures and checkout drop-offs across
5 fake customers, spread across the last hour so timestamps look natural.

Usage:
    python scripts/seed_demo_data.py

Requires the server running locally (uvicorn app.main:app --reload).
"""
import hashlib
import hmac
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = "http://localhost:8000"
WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

CUSTOMERS = [
    {"id": "cust_demo_001", "name": "Priya Sharma",    "sub": "sub_demo_001"},
    {"id": "cust_demo_002", "name": "Rahul Mehta",     "sub": "sub_demo_002"},
    {"id": "cust_demo_003", "name": "Anjali Singh",    "sub": "sub_demo_003"},
    {"id": "cust_demo_004", "name": "Vikram Nair",     "sub": "sub_demo_004"},
    {"id": "cust_demo_005", "name": "Deepa Reddy",     "sub": "sub_demo_005"},
]

FAILURE_SCENARIOS = [
    # (error_code, error_reason, amount_paise, attempts)
    ("BAD_REQUEST_ERROR", "insufficient funds in account",          99900,  1),
    ("BAD_REQUEST_ERROR", "insufficient funds in account",          49900,  2),
    ("GATEWAY_ERROR",     "card_expired",                           149900, 1),
    ("GATEWAY_ERROR",     "risk engine flagged transaction as fraud",299900, 1),
    ("BAD_REQUEST_ERROR", "insufficient funds in account",          99900,  4),  # halted
    ("GATEWAY_ERROR",     "transaction declined by issuing bank",   199900, 1),  # ambiguous
    ("BAD_REQUEST_ERROR", "insufficient funds in account",          999900, 1),  # high value
    ("GATEWAY_ERROR",     "network timeout",                        79900,  1),
]

def sign(body_bytes: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()


def send_failure(customer: dict, scenario: tuple, payment_id: str) -> dict:
    error_code, error_reason, amount, attempts = scenario
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "customer_id": customer["id"],
                    "subscription_id": customer["sub"],
                    "error_code": error_code,
                    "error_reason": error_reason,
                }
            },
            "subscription": {
                "entity": {
                    "id": customer["sub"],
                    "payment_attempts": attempts,
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    resp = httpx.post(
        f"{SERVER_URL}/webhooks/razorpay",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign(body),
        },
        timeout=15.0,
    )
    return resp.json()


def send_abandonment(customer: dict, amount: float) -> dict:
    resp = httpx.post(
        f"{SERVER_URL}/debug/simulate-abandonment",
        json={
            "customer_id": customer["id"],
            "subscription_id": customer["sub"],
            "amount_inr": amount,
            "checkout_status": "attempted",
        },
        timeout=15.0,
    )
    return resp.json()


def seed():
    if not WEBHOOK_SECRET:
        print("ERROR: RAZORPAY_WEBHOOK_SECRET not set in .env")
        sys.exit(1)

    print("Seeding demo data...\n")
    total = 0

    # --- Payment failures across all customers ---
    events = [
        (CUSTOMERS[0], FAILURE_SCENARIOS[0], "pay_demo_001"),  # insufficient funds x1
        (CUSTOMERS[1], FAILURE_SCENARIOS[2], "pay_demo_002"),  # card expired
        (CUSTOMERS[2], FAILURE_SCENARIOS[3], "pay_demo_003"),  # risk block
        (CUSTOMERS[3], FAILURE_SCENARIOS[4], "pay_demo_004"),  # halted (4 attempts)
        (CUSTOMERS[4], FAILURE_SCENARIOS[6], "pay_demo_005"),  # high value
        (CUSTOMERS[0], FAILURE_SCENARIOS[1], "pay_demo_006"),  # insufficient funds x2
        (CUSTOMERS[1], FAILURE_SCENARIOS[5], "pay_demo_007"),  # ambiguous decline
        (CUSTOMERS[2], FAILURE_SCENARIOS[7], "pay_demo_008"),  # network error
    ]

    for customer, scenario, pay_id in events:
        result = send_failure(customer, scenario, pay_id)
        action = result.get("decision", {}).get("action", "unknown")
        print(f"  ✓ Payment failure [{scenario[1][:30]}...] → {action}")
        total += 1
        time.sleep(0.3)  # avoid rate limiting Groq

    print()

    # --- Checkout drop-offs: escalation ladder for 2 customers ---
    # Customer 3: goes through full ladder (1st → 2nd → 3rd abandonment)
    for i in range(3):
        result = send_abandonment(CUSTOMERS[2], 999.0)
        action = result.get("decision", {}).get("action", "unknown")
        count = result.get("abandonment_count", i + 1)
        print(f"  ✓ Drop-off [{CUSTOMERS[2]['name']}] abandonment #{count} → {action}")
        total += 1
        time.sleep(0.3)

    print()

    # Customer 4: 2 abandonments (shows reminder → incentive)
    for i in range(2):
        result = send_abandonment(CUSTOMERS[3], 1499.0)
        action = result.get("decision", {}).get("action", "unknown")
        count = result.get("abandonment_count", i + 1)
        print(f"  ✓ Drop-off [{CUSTOMERS[3]['name']}] abandonment #{count} → {action}")
        total += 1
        time.sleep(0.3)

    print()

    # Customer 5: 1 abandonment (just a reminder)
    result = send_abandonment(CUSTOMERS[4], 499.0)
    action = result.get("decision", {}).get("action", "unknown")
    print(f"  ✓ Drop-off [{CUSTOMERS[4]['name']}] abandonment #1 → {action}")
    total += 1

    print(f"\nDone — {total} events seeded.")
    print("Open the dashboard to see the data.")


if __name__ == "__main__":
    seed()

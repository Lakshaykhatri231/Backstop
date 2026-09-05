"""
Simulates Razorpay webhook deliveries against your local server so you
can test/demo the full pipeline before (or without) a live Razorpay
webhook actually firing.

Usage:
    python scripts/simulate_webhook.py insufficient_funds
    python scripts/simulate_webhook.py card_expired
    python scripts/simulate_webhook.py risk_block
    python scripts/simulate_webhook.py halted
    python scripts/simulate_webhook.py high_value

Requires the server running locally (uvicorn app.main:app --reload)
and RAZORPAY_WEBHOOK_SECRET set the same in your .env as used here.
"""
import hashlib
import hmac
import json
import sys
import time
import os
import uuid
from dotenv import load_dotenv

import httpx
load_dotenv()

SERVER_URL = "http://localhost:8000/webhooks/razorpay"

# Must match RAZORPAY_WEBHOOK_SECRET in your .env
WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

SCENARIOS = {
    "insufficient_funds": {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_if001",
                    "amount": 99900,  # paise -> 999.00 INR
                    "customer_id": "cust_test_001",
                    "subscription_id": "sub_test_001",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "insufficient funds in account",
                }
            },
            "subscription": {"entity": {"id": "sub_test_001", "payment_attempts": 1}},
        },
    },
    "card_expired": {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_ce001",
                    "amount": 49900,
                    "customer_id": "cust_test_002",
                    "subscription_id": "sub_test_002",
                    "error_code": "GATEWAY_ERROR",
                    "error_reason": "card_expired",
                }
            },
            "subscription": {"entity": {"id": "sub_test_002", "payment_attempts": 2}},
        },
    },
    "risk_block": {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_rb001",
                    "amount": 150000,
                    "customer_id": "cust_test_003",
                    "subscription_id": "sub_test_003",
                    "error_code": "GATEWAY_ERROR",
                    "error_reason": "risk engine flagged transaction as fraud",
                }
            },
            "subscription": {"entity": {"id": "sub_test_003", "payment_attempts": 1}},
        },
    },
    "halted": {
        "event": "subscription.halted",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_h001",
                    "amount": 99900,
                    "customer_id": "cust_test_001",
                    "subscription_id": "sub_test_001",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "insufficient funds in account",
                }
            },
            "subscription": {"entity": {"id": "sub_test_001", "payment_attempts": 4}},
        },
    },
    "high_value": {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_hv001",
                    "amount": 999900,  # 9999 INR - above default high-value threshold
                    "customer_id": "cust_test_004",
                    "subscription_id": "sub_test_004",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "insufficient funds in account",
                }
            },
            "subscription": {"entity": {"id": "sub_test_004", "payment_attempts": 1}},
        },
    },
    "ambiguous_decline": {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_ad001",
                    "amount": 199900,
                    "customer_id": "cust_test_005",
                    "subscription_id": "sub_test_005",
                    "error_code": "GATEWAY_ERROR",
                    "error_reason": "transaction declined by issuing bank",
                }
            },
            "subscription": {"entity": {"id": "sub_test_005", "payment_attempts": 1}},
        },
    },

}


def sign(body_bytes: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def send(scenario_name: str, event_id: str | None = None):
    if not WEBHOOK_SECRET:
        print(
            "ERROR: RAZORPAY_WEBHOOK_SECRET is not set (or empty) in your .env file.\n"
            "Set it to any non-empty string, e.g.:\n"
            "  RAZORPAY_WEBHOOK_SECRET=test_secret_123\n"
            "then restart the uvicorn server so it picks up the change, and re-run this script."
        )
        sys.exit(1)

    if scenario_name not in SCENARIOS:
        print(f"Unknown scenario '{scenario_name}'. Options: {list(SCENARIOS.keys())}")
        sys.exit(1)

    body = json.dumps(SCENARIOS[scenario_name]).encode("utf-8")
    signature = sign(body)
    # Real Razorpay webhooks carry a unique event ID in this header - it's
    # what app/webhook.py now uses for duplicate-delivery detection. Pass the
    # same event_id twice (via --event-id) to test that dedup logic yourself.
    event_id = event_id or f"evt_sim_{uuid.uuid4().hex[:20]}"

    resp = httpx.post(
        SERVER_URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
        timeout=15.0,
    )
    print(f"Sent with X-Razorpay-Event-Id: {event_id}")
    print(f"Status: {resp.status_code}")
    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python simulate_webhook.py <scenario> [event_id]\nOptions: {list(SCENARIOS.keys())}")
        print("Pass the same event_id twice to test duplicate-webhook handling, e.g.:")
        print("  python simulate_webhook.py insufficient_funds evt_dedup_test_1")
        print("  python simulate_webhook.py insufficient_funds evt_dedup_test_1   # should be ignored as duplicate")
        sys.exit(1)
    send(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)

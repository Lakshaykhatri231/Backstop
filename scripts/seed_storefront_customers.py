"""
Seeds 5 storefront customers with realistic, backdated order history so each
one's tier is stable *before* the demo starts - no need to rely on live
recomputation mid-pitch.

This writes directly to the DB (not via the HTTP API) purely so timestamps
can be backdated realistically. Safe to re-run: it skips any customer whose
email already exists.

Usage:
    python scripts/seed_storefront_customers.py

Requires DATABASE_URL to be set (.env) - does NOT require the server to be
running, since it talks to the DB directly rather than through HTTP.
"""
import random
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Make sure the project root (parent of this scripts/ folder) is on sys.path,
# so `python scripts/seed_storefront_customers.py` works from any directory
# without needing PYTHONPATH set manually.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, engine, SessionLocal
from app.models import Customer, Order, OrderStatus, FailureReason, CartEvent, CartEventType, AgentAction
from app.auth import hash_password
from app.tiering import refresh_tier

DEMO_PASSWORD = "password123"

random.seed(42)  # stable output across re-runs


def days_ago(n: int, hour: int = 12) -> datetime:
    return datetime.utcnow() - timedelta(days=n, hours=-hour)


def make_customer(db, email, name):
    existing = db.query(Customer).filter(Customer.email == email).first()
    if existing:
        print(f"  - {email} already exists, skipping")
        return existing
    customer = Customer(email=email, password_hash=hash_password(DEMO_PASSWORD), name=name,
                         created_at=days_ago(60))
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def make_order(db, customer, amount_inr, status, failure_reason=None, days_back=0):
    order = Order(
        # Seeded history is settled history: never an open revenue/risk item.
        revenue_recorded=True, risk_settled=True,
        customer_id=customer.id,
        razorpay_order_id=f"order_seed_{customer.email.split('@')[0]}_{random.randint(1000,9999)}",
        items_json="[]",
        amount_inr=amount_inr,
        status=status,
        failure_reason=failure_reason,
        created_at=days_ago(days_back),
        resolved_at=days_ago(days_back) if status != OrderStatus.CREATED else None,
    )
    db.add(order)
    db.commit()


def make_cancel_event(db, customer, amount_inr, days_back):
    db.add(CartEvent(
        customer_id=customer.id,
        event_type=CartEventType.EXPLICIT_CANCEL,
        items_json="[]",
        amount_inr=amount_inr,
        tier_at_time=customer.tier,
        action=AgentAction.NO_ACTION,
        confidence=0.75,
        reasoning="Seeded historical cancel for demo purposes.",
        outcome="logged",
        created_at=days_ago(days_back),
    ))
    db.commit()


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    print("Seeding storefront customers...\n")

    # 1. Priya - trusted: 8 orders, 7 captured, 1 failed (card_expired)
    priya = make_customer(db, "priya@demo.com", "Priya Sharma")
    for i in range(7):
        make_order(db, priya, random.choice([1499, 2499, 3999, 5999]), OrderStatus.CAPTURED, days_back=50 - i * 6)
    make_order(db, priya, 3999, OrderStatus.FAILED, FailureReason.CARD_EXPIRED, days_back=8)
    refresh_tier(db, priya)
    print(f"  ✓ priya@demo.com -> tier: {priya.tier.value}")

    # 2. Arjun - standard: 3 orders, 2 captured, 1 failed
    arjun = make_customer(db, "arjun@demo.com", "Arjun Kapoor")
    make_order(db, arjun, 899, OrderStatus.CAPTURED, days_back=30)
    make_order(db, arjun, 2499, OrderStatus.CAPTURED, days_back=15)
    make_order(db, arjun, 1499, OrderStatus.FAILED, FailureReason.INSUFFICIENT_FUNDS, days_back=4)
    refresh_tier(db, arjun)
    print(f"  ✓ arjun@demo.com -> tier: {arjun.tier.value}")

    # 3. Neha - risk: 5 orders, 1 captured, 4 failed (2x risk_block)
    neha = make_customer(db, "neha@demo.com", "Neha Verma")
    make_order(db, neha, 5999, OrderStatus.CAPTURED, days_back=45)
    make_order(db, neha, 8999, OrderStatus.FAILED, FailureReason.RISK_BLOCK, days_back=20)
    make_order(db, neha, 3999, OrderStatus.FAILED, FailureReason.RISK_BLOCK, days_back=12)
    make_order(db, neha, 1499, OrderStatus.FAILED, FailureReason.INSUFFICIENT_FUNDS, days_back=6)
    make_order(db, neha, 899, OrderStatus.FAILED, FailureReason.INSUFFICIENT_FUNDS, days_back=2)
    refresh_tier(db, neha)
    print(f"  ✓ neha@demo.com -> tier: {neha.tier.value}")

    # 4. Rahul - new: no order history at all
    rahul = make_customer(db, "rahul@demo.com", "Rahul Iyer")
    refresh_tier(db, rahul)
    print(f"  ✓ rahul@demo.com -> tier: {rahul.tier.value}")

    # 5. Zara - standard, but a serial-canceller: 3 captured orders + 3 explicit cancels
    zara = make_customer(db, "zara@demo.com", "Zara Khan")
    make_order(db, zara, 1499, OrderStatus.CAPTURED, days_back=25)
    make_order(db, zara, 2499, OrderStatus.CAPTURED, days_back=14)
    make_order(db, zara, 899, OrderStatus.CAPTURED, days_back=5)
    refresh_tier(db, zara)
    make_cancel_event(db, zara, 3999, days_back=20)
    make_cancel_event(db, zara, 5999, days_back=10)
    make_cancel_event(db, zara, 1499, days_back=3)
    print(f"  ✓ zara@demo.com -> tier: {zara.tier.value} (3 prior explicit cancels - watch the 4th trigger escalation)")

    db.close()

    print(f"\nAll seed accounts use password: {DEMO_PASSWORD}")
    print("Log in at http://localhost:8000/store with any of the emails above.")
    print("\nSuggested demo script:")
    print("  - priya@demo.com  (trusted) -> add an item, simulate cart timeout -> offer_incentive")
    print("  - neha@demo.com   (risk)    -> add an item, delete cart -> no_action (not worth chasing)")
    print("  - rahul@demo.com  (new)     -> add an item, simulate cart timeout -> send_reminder, no incentive")
    print("  - zara@demo.com   (standard, serial-canceller) -> delete cart -> escalate_to_human (4th cancel)")


if __name__ == "__main__":
    seed()

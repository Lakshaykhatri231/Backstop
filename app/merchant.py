from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Customer, CartEvent, Order, OrderStatus
from app.revenue import get_or_create_state, as_dict as revenue_as_dict
from app.tiering import customer_stats

router = APIRouter()


@router.get("/merchant/revenue")
def merchant_revenue(db: Session = Depends(get_db)):
    state = get_or_create_state(db)
    return revenue_as_dict(state)


@router.get("/merchant/customers")
def merchant_customers(db: Session = Depends(get_db), limit: int = 50):
    customers = db.query(Customer).order_by(Customer.created_at.desc()).limit(limit).all()
    return [
        {
            "id": c.id,
            "email": c.email,
            "name": c.name,
            "tier": c.tier.value,
            "created_at": c.created_at,
            "stats": customer_stats(db, c.id),
        }
        for c in customers
    ]


@router.get("/merchant/cart-events")
def merchant_cart_events(db: Session = Depends(get_db), limit: int = 50):
    events = db.query(CartEvent).order_by(CartEvent.created_at.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "customer_id": e.customer_id,
            "customer_name": e.customer.name if e.customer else None,
            "event_type": e.event_type.value,
            "amount_inr": e.amount_inr,
            "tier_at_time": e.tier_at_time.value,
            "action": e.action.value if e.action else None,
            "confidence": e.confidence,
            "reasoning": e.reasoning,
            "outcome": e.outcome,
            "status": e.status.value if e.status else None,
            "incentive_pct": e.incentive_pct,
            "final_amount_inr": e.incentive_final_amount_inr,
            "created_at": e.created_at,
            "resolved_at": e.resolved_at,
        }
        for e in events
    ]


@router.get("/merchant/orders")
def merchant_orders(db: Session = Depends(get_db), limit: int = 50):
    orders = db.query(Order).order_by(Order.created_at.desc()).limit(limit).all()
    return [
        {
            "id": o.id,
            "customer_name": o.customer.name if o.customer else None,
            "razorpay_order_id": o.razorpay_order_id,
            "amount_inr": o.amount_inr,
            "status": o.status.value,
            "failure_reason": o.failure_reason.value if o.failure_reason else None,
            "recovered_from_cart_event_id": o.recovered_from_cart_event_id,
            "created_at": o.created_at,
            "resolved_at": o.resolved_at,
        }
        for o in orders
    ]

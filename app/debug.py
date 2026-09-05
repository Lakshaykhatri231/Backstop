"""
Debug endpoints for demoing the full pipeline on demand without needing
a live Razorpay account or real checkout traffic.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sqlalchemy.orm import Session

from app.database import get_db
from app.runtime_flags import set_llm_failure_forced, is_llm_failure_forced
from app.dropoff import process_abandoned_order

router = APIRouter()


class ToggleRequest(BaseModel):
    forced: bool


class SimulateAbandonmentRequest(BaseModel):
    customer_id: str = "cust_test_001"
    subscription_id: str | None = "sub_test_001"
    amount_inr: float = 999.0
    checkout_status: str = "attempted"   # "attempted" | "created"
    abandonment_count_override: int | None = None  # if set, skips DB count lookup


@router.post("/debug/toggle-llm-failure")
def toggle_llm_failure(req: ToggleRequest):
    """
    Flip LLM failure simulation on/off live during a demo.
    Flip on → send any event → see rules_engine_fallback in the response.
    Flip off → normal LLM path resumes.
    """
    set_llm_failure_forced(req.forced)
    return {"llm_failure_forced": is_llm_failure_forced()}


@router.post("/debug/simulate-abandonment")
def simulate_abandonment(req: SimulateAbandonmentRequest, db: Session = Depends(get_db)):
    """
    Inject a fake checkout abandonment event directly into the pipeline,
    bypassing the 10-minute poller wait and the Razorpay API call.

    Use this to demo the full drop-off escalation ladder live:
      abandonment_count_override=1  → send_reminder
      abandonment_count_override=2  → send_resume_link or offer_incentive
      abandonment_count_override=3  → escalate_to_human

    The full decide → gate → execute → audit pipeline runs unchanged —
    only the signal source (poller vs this endpoint) differs.
    """
    result = process_abandoned_order(
        db=db,
        razorpay_order_id=f"order_demo_{req.customer_id}_{int(__import__('time').time())}",
        customer_id=req.customer_id,
        subscription_id=req.subscription_id,
        amount_inr=req.amount_inr,
        checkout_status=req.checkout_status,
        minutes_since_created=11,  # just over the 10-min abandonment window
    )
    return result

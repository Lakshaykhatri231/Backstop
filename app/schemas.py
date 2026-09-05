from pydantic import BaseModel
from typing import Optional


class RazorpayWebhookPayload(BaseModel):
    """Minimal shape we actually read out of a Razorpay webhook body.
    Real payloads have more nesting; we only pull what we need."""
    event: str
    payload: dict


class DecisionResult(BaseModel):
    action: str
    confidence: float
    reasoning: str
    source: str
    escalated: bool


class OutcomeSummary(BaseModel):
    total_events: int
    total_decisions: int
    nudges_sent: int
    escalated: int
    escalated_by_confidence_gate: int
    retries: int
    no_action: int
    failed: int
    other: int

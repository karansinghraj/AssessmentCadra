"""API response models used only for OpenAPI/Swagger documentation.

The brief's contract models live in `schema/response_schema.py` and are left
untouched. These subclasses add the run-level fields the service returns in
addition to the contract (action, state, decisions, trace extras). FastAPI uses
them to render a complete Swagger schema and to validate outgoing payloads.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from schema.response_schema import LedgerEntry, TicketResponse, Trace


class ActionModel(BaseModel):
    action: Literal["REFUND", "REPLY", "ESCALATION"]
    amount_usd: Optional[float] = None
    line_no: Optional[int] = None
    item: Optional[str] = None
    status: Literal["PROPOSED", "APPROVED", "REJECTED"]


class DecisionRecord(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str
    decided_at: str


class TraceModel(Trace):
    latency_ms: int = 0
    tokens_total: int = 0
    pricing: dict[str, Any] = Field(default_factory=dict)
    classification_path: Optional[str] = None
    classification_route: Optional[str] = None
    classifier_used: bool = False
    judge: Optional[dict[str, Any]] = None
    sanity_issues: List[str] = Field(default_factory=list)
    calls_detail: List[dict[str, Any]] = Field(default_factory=list)
    processed_at: Optional[str] = None


class RunResponse(TicketResponse):
    action: ActionModel
    state: Literal["AWAITING_APPROVAL", "APPROVED", "REJECTED", "ESCALATED", "CLOSED"]
    decisions: List[DecisionRecord] = Field(default_factory=list)
    decision: Optional[DecisionRecord] = None
    trace: TraceModel


class LedgerRecord(LedgerEntry):
    approver: Optional[str] = None
    decided_at: Optional[str] = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorResponse(BaseModel):
    detail: str
    request_id: Optional[str] = None

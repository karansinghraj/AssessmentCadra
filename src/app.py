"""FastAPI service for the Larkfield Home email triage agent.

Endpoints:
  GET  /health
  POST /tickets
  GET  /runs/{run_id}
  POST /runs/{run_id}/decision
  GET  /ledger

Interactive documentation is served at /docs (Swagger UI) and /redoc, with the
machine-readable schema at /openapi.json.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from schema.response_schema import Decision, Ticket

from .api_models import ErrorResponse, HealthResponse, LedgerRecord, RunResponse
from .config import get_config
from .logging_config import configure_logging
from .middleware import RequestContextMiddleware
from .normalize import iso_now
from .pipeline import process_ticket
from .prepare_data import ensure_built
from .store import Store

logger = logging.getLogger("larkfield.api")

OPENAPI_TAGS = [
    {
        "name": "system",
        "description": "Liveness and schema checks. `/health` is the deploy health check.",
    },
    {
        "name": "tickets",
        "description": "Submit an email ticket. The same `ticket_id` is idempotent and returns one run.",
    },
    {
        "name": "runs",
        "description": "Fetch a run and record the human decision for a PEND run.",
    },
    {
        "name": "ledger",
        "description": "Read the append-only action ledger. One row per run/action.",
    },
]


def create_app() -> FastAPI:
    cfg = get_config()
    configure_logging()
    data = ensure_built(cfg.data_dir, cfg.build_dir)
    store = Store(cfg.run_db_path)

    app = FastAPI(
        title="Larkfield Home Support Agent",
        version="1.0.0",
        summary="Email triage that proposes support decisions without mutating customer data.",
        description=(
            "Reads one customer-support email and returns the desk decision: outcome, checks, "
            "reason codes, proposed action and a draft reply.\n\n"
            "**Safety contract:** the service never refunds, sends email or changes customer "
            "data. It only writes a run and an action-ledger row. PEND runs require a human "
            "decision; decisions are idempotent and a conflicting decision returns HTTP 409.\n\n"
            "Pattern routing handles common mail without a model call. The optional model "
            "classifier is used only for low-confidence mail and the optional judge can only "
            "block a run. Money is always computed deterministically."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.cfg = cfg
    app.state.data = data
    app.state.store = store
    app.add_middleware(RequestContextMiddleware)

    logger.info(
        "app_started",
        extra={
            "event": "app_started",
            "llm_configured": cfg.llm_configured,
            "llm_model": cfg.llm_model if cfg.llm_configured else "rule-based-fallback",
            "db_path": str(cfg.run_db_path),
            "orders": len(data.get("orders", {})),
            "customers": len(data.get("customers", {})),
        },
    )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        logger.exception(
            "unhandled_exception",
            extra={
                "event": "unhandled_exception",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_error", "request_id": request_id},
        )

    @app.get(
        "/health",
        tags=["system"],
        response_model=HealthResponse,
        summary="Liveness probe",
        description="Returns exactly `{\"status\":\"ok\"}` when the service is ready.",
    )
    def health():
        return {"status": "ok"}

    @app.post(
        "/tickets",
        tags=["tickets"],
        response_model=RunResponse,
        summary="Triage one support email",
        description=(
            "Validates the contract ticket, runs the pattern/LLM/policy pipeline and persists one "
            "run plus one ledger row. Re-posting the same `ticket_id` returns the existing run and "
            "does not create a second ledger row."
        ),
        responses={500: {"model": ErrorResponse, "description": "Internal error"}},
    )
    def create_ticket(ticket: Ticket):
        payload = ticket.model_dump(by_alias=True)
        existing = store.get_run_by_ticket(payload["ticket_id"])
        if existing:
            logger.info(
                "ticket_reused",
                extra={
                    "event": "ticket_reused",
                    "ticket_id": payload["ticket_id"],
                    "run_id": existing.get("run_id"),
                    "outcome": existing.get("outcome"),
                    "state": existing.get("state"),
                },
            )
            return existing
        response = process_ticket(payload, data, store, cfg)
        recorded = store.create_run(
            run_id=response["run_id"],
            ticket_id=payload["ticket_id"],
            ticket=payload,
            response=response,
            state=response["state"],
        )
        trace = response.get("trace") or {}
        logger.info(
            "ticket_processed",
            extra={
                "event": "ticket_processed",
                "ticket_id": payload["ticket_id"],
                "run_id": recorded.get("run_id"),
                "ticket_type": recorded.get("ticket_type"),
                "outcome": recorded.get("outcome"),
                "state": recorded.get("state"),
                "approver_role": recorded.get("approver_role"),
                "amount_usd": (recorded.get("action") or {}).get("amount_usd"),
                "classification_path": trace.get("classification_path"),
                "latency_ms": trace.get("latency_ms"),
                "cost_usd": trace.get("cost_usd"),
                "tokens_total": trace.get("tokens_total"),
            },
        )
        return recorded

    @app.get(
        "/runs/{run_id}",
        tags=["runs"],
        response_model=RunResponse,
        summary="Fetch a run",
        description="Returns the stored run, including runs waiting for a human decision.",
        responses={404: {"model": ErrorResponse, "description": "Run not found"}},
    )
    def get_run(run_id: str):
        run = store.get_run(run_id)
        if not run:
            logger.warning(
                "run_not_found",
                extra={"event": "run_not_found", "run_id": run_id},
            )
            raise HTTPException(status_code=404, detail="run_not_found")
        logger.info(
            "run_fetched",
            extra={
                "event": "run_fetched",
                "run_id": run_id,
                "ticket_id": run.get("ticket_id"),
                "state": run.get("state"),
                "outcome": run.get("outcome"),
            },
        )
        return run

    @app.post(
        "/runs/{run_id}/decision",
        tags=["runs"],
        response_model=RunResponse,
        summary="Record a human decision",
        description=(
            "Approves or rejects a run in `AWAITING_APPROVAL`. The first decision wins. Repeating "
            "the same decision is idempotent; a different decision returns HTTP 409."
        ),
        responses={
            404: {"model": ErrorResponse, "description": "Run not found"},
            409: {"model": ErrorResponse, "description": "Run already decided or not awaiting approval"},
            422: {"model": ErrorResponse, "description": "Approver is required"},
        },
    )
    def decide(run_id: str, decision: Decision):
        approver = decision.approver.strip()
        if not approver:
            raise HTTPException(status_code=422, detail="approver_required")
        run = store.get_run(run_id)
        if not run:
            logger.warning(
                "decision_run_not_found",
                extra={"event": "decision_run_not_found", "run_id": run_id},
            )
            raise HTTPException(status_code=404, detail="run_not_found")

        if run["state"] in {"APPROVED", "REJECTED"}:
            existing = (run.get("decisions") or [{}])[0]
            if existing.get("decision") == decision.decision:
                logger.info(
                    "decision_reused",
                    extra={
                        "event": "decision_reused",
                        "run_id": run_id,
                        "ticket_id": run.get("ticket_id"),
                        "decision": decision.decision,
                        "state": run["state"],
                    },
                )
                return run
            logger.warning(
                "decision_conflict",
                extra={
                    "event": "decision_conflict",
                    "run_id": run_id,
                    "ticket_id": run.get("ticket_id"),
                    "requested": decision.decision,
                    "existing": existing.get("decision"),
                    "state": run["state"],
                },
            )
            raise HTTPException(status_code=409, detail="run_already_decided")
        if run["state"] != "AWAITING_APPROVAL":
            logger.warning(
                "decision_not_waiting",
                extra={
                    "event": "decision_not_waiting",
                    "run_id": run_id,
                    "ticket_id": run.get("ticket_id"),
                    "state": run["state"],
                },
            )
            raise HTTPException(status_code=409, detail="run_not_waiting_for_approval")

        inserted, info = store.add_decision(run_id, decision.decision, approver)
        if not inserted:
            existing = (store.get_run(run_id) or {}).get("decisions") or [{}]
            if existing[0].get("decision") != decision.decision:
                logger.warning(
                    "decision_conflict",
                    extra={
                        "event": "decision_conflict",
                        "run_id": run_id,
                        "ticket_id": run.get("ticket_id"),
                        "requested": decision.decision,
                        "existing": existing[0].get("decision"),
                    },
                )
                raise HTTPException(status_code=409, detail="run_already_decided")
            return store.get_run(run_id)

        new_state = "APPROVED" if decision.decision == "approve" else "REJECTED"
        updated = dict(run)
        updated["state"] = new_state
        updated["human_approval_required"] = False
        updated["approver_role"] = None
        updated["decisions"] = [info]
        updated["decision"] = info
        store.save_response(run_id, updated, new_state)

        action = updated.get("action") or {}
        store.update_ledger(
            run_id=run_id,
            action=action.get("action") or "REFUND",
            status="APPROVED" if decision.decision == "approve" else "REJECTED",
            approver=approver,
            decided_at=iso_now(),
        )
        logger.info(
            "decision_recorded",
            extra={
                "event": "decision_recorded",
                "run_id": run_id,
                "ticket_id": run.get("ticket_id"),
                "decision": decision.decision,
                "state": new_state,
                "approver": approver,
                "action": action.get("action"),
                "amount_usd": action.get("amount_usd"),
            },
        )
        return store.get_run(run_id)

    @app.get(
        "/ledger",
        tags=["ledger"],
        response_model=list[LedgerRecord],
        summary="Read the action ledger",
        description="Returns one row per run/action with status, amount and decision metadata.",
    )
    def ledger():
        entries = store.list_ledger()
        for entry in entries:
            if entry.get("amount_usd") is not None:
                entry["amount_usd"] = round(float(entry["amount_usd"]), 2)
        logger.info(
            "ledger_listed",
            extra={"event": "ledger_listed", "rows": len(entries)},
        )
        return entries

    return app


app = create_app()

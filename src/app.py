"""FastAPI service for the Larkfield Home email triage agent.

Endpoints:
  GET  /health
  POST /tickets
  GET  /runs/{run_id}
  POST /runs/{run_id}/decision
  GET  /ledger
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from schema.response_schema import Decision, Ticket

from .config import get_config
from .middleware import RequestContextMiddleware
from .normalize import iso_now
from .pipeline import process_ticket
from .prepare_data import ensure_built
from .store import Store


def create_app() -> FastAPI:
    cfg = get_config()
    data = ensure_built(cfg.data_dir, cfg.build_dir)
    store = Store(cfg.run_db_path)

    app = FastAPI(
        title="Larkfield Home Support Agent",
        version="1.0.0",
        description="Proposes support decisions; never refunds, sends or changes customer data.",
    )
    app.state.cfg = cfg
    app.state.data = data
    app.state.store = store
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_error", "request_id": request_id},
        )

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok"}

    @app.post("/tickets", tags=["tickets"])
    def create_ticket(ticket: Ticket):
        payload = ticket.model_dump(by_alias=True)
        existing = store.get_run_by_ticket(payload["ticket_id"])
        if existing:
            return existing
        response = process_ticket(payload, data, store, cfg)
        return store.create_run(
            run_id=response["run_id"],
            ticket_id=payload["ticket_id"],
            ticket=payload,
            response=response,
            state=response["state"],
        )

    @app.get("/runs/{run_id}", tags=["runs"])
    def get_run(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run_not_found")
        return run

    @app.post("/runs/{run_id}/decision", tags=["runs"])
    def decide(run_id: str, decision: Decision):
        if not decision.approver.strip():
            raise HTTPException(status_code=422, detail="approver_required")
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run_not_found")

        if run["state"] in {"APPROVED", "REJECTED"}:
            existing = (run.get("decisions") or [{}])[0]
            if existing.get("decision") == decision.decision:
                return run
            raise HTTPException(status_code=409, detail="run_already_decided")
        if run["state"] != "AWAITING_APPROVAL":
            raise HTTPException(status_code=409, detail="run_not_waiting_for_approval")

        inserted, info = store.add_decision(run_id, decision.decision, decision.approver.strip())
        if not inserted:
            existing = (store.get_run(run_id) or {}).get("decisions") or [{}]
            if existing[0].get("decision") != decision.decision:
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
            approver=decision.approver.strip(),
            decided_at=iso_now(),
        )
        return store.get_run(run_id)

    @app.get("/ledger", tags=["ledger"])
    def ledger():
        entries = store.list_ledger()
        for entry in entries:
            if entry.get("amount_usd") is not None:
                entry["amount_usd"] = round(float(entry["amount_usd"]), 2)
        return entries

    return app


app = create_app()

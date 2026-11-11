"""HTTP contract and idempotency tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from schema.response_schema import TicketResponse

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setenv("LLM_API_KEY", "")
    from src.app import create_app

    return TestClient(create_app())


def _tickets() -> dict[str, dict]:
    data = json.loads((ROOT / "tickets" / "support_inbox_export.json").read_text(encoding="utf-8"))
    return {ticket["ticket_id"]: ticket for ticket in data}


def test_health_exact(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_ticket_and_fetch_waiting_run(client):
    response = client.post("/tickets", json=_tickets()["T001"])
    assert response.status_code == 200
    body = response.json()
    TicketResponse.model_validate(body)
    assert body["outcome"] == "PEND"
    assert body["state"] == "AWAITING_APPROVAL"
    assert body["human_approval_required"] is True
    fetched = client.get(f"/runs/{body['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "AWAITING_APPROVAL"


def test_decision_is_recorded_once(client):
    body = client.post("/tickets", json=_tickets()["T001"]).json()
    run_id = body["run_id"]
    first = client.post(f"/runs/{run_id}/decision", json={"decision": "approve", "approver": "lead"})
    second = client.post(f"/runs/{run_id}/decision", json={"decision": "approve", "approver": "lead"})
    conflict = client.post(f"/runs/{run_id}/decision", json={"decision": "reject", "approver": "other"})
    assert first.status_code == 200
    assert first.json()["state"] == "APPROVED"
    assert second.status_code == 200
    assert conflict.status_code == 409
    rows = [row for row in client.get("/ledger").json() if row["run_id"] == run_id]
    assert len(rows) == 1
    assert rows[0]["status"] == "APPROVED"
    assert rows[0]["amount_usd"] == 210.0


def test_same_ticket_reuses_run_and_ledger_row(client):
    ticket = _tickets()["T002"]
    first = client.post("/tickets", json=ticket).json()
    second = client.post("/tickets", json=ticket).json()
    assert first["run_id"] == second["run_id"]
    rows = [row for row in client.get("/ledger").json() if row["ticket_id"] == "T002"]
    assert len(rows) == 1


def test_injection_escalates_and_never_refunds(client):
    body = client.post("/tickets", json=_tickets()["T020"]).json()
    assert body["outcome"] == "ESCALATE"
    assert body["approver_role"] == "TRUST_SAFETY"
    assert body["action"]["action"] == "ESCALATION"
    assert body["action"]["amount_usd"] is None


def test_whole_order_is_modified(client):
    body = client.post("/tickets", json=_tickets()["T019"]).json()
    assert body["outcome"] == "PROCEED_MODIFIED"
    assert body["action"]["amount_usd"] == 27.5
    assert "PARTIAL_LINES" in body["reason_codes"]

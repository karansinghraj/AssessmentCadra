"""End-to-end dry run for the Larkfield support agent.

Run:  python -m scripts.dry_run

Exercises the real FastAPI app through TestClient: health/docs/OpenAPI, a PEND
ticket, run fetch, approve, idempotent repeat, conflicting decision, a PROCEED
ticket, ticket-idempotency, an injection escalation, ledger invariants, request
headers and JSON logging. Uses a temporary database and no model key.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _tickets() -> dict[str, dict]:
    rows = json.loads((ROOT / "tickets" / "support_inbox_export.json").read_text(encoding="utf-8"))
    return {row["ticket_id"]: row for row in rows}


def main() -> int:
    os.environ["RUN_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="larkfield_dry_")) / "runs.db")
    os.environ["LLM_API_KEY"] = ""
    os.environ["LOG_LEVEL"] = "WARNING"

    from fastapi.testclient import TestClient

    from src.app import create_app

    app = create_app()

    # Capture structured API logs without printing them over the step table.
    capture = Capture()
    api_logger = logging.getLogger("larkfield.api")
    api_logger.handlers.clear()
    api_logger.addHandler(capture)
    api_logger.setLevel(logging.INFO)
    api_logger.propagate = False

    client = TestClient(app)
    tickets = _tickets()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    # 1. Docs and liveness -------------------------------------------------
    health = client.get("/health")
    check("health exact", health.status_code == 200 and health.json() == {"status": "ok"}, health.text)

    schema_response = client.get("/openapi.json")
    schema = schema_response.json()
    expected_paths = {"/health", "/tickets", "/runs/{run_id}", "/runs/{run_id}/decision", "/ledger"}
    check("openapi paths", schema_response.status_code == 200 and set(schema["paths"]) == expected_paths)
    ticket_ref = (
        schema["paths"]["/tickets"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    )
    check("openapi run response model", ticket_ref.get("$ref") == "#/components/schemas/RunResponse")
    docs = client.get("/docs")
    check("swagger ui", docs.status_code == 200 and "swagger-ui" in docs.text.lower())

    # 2. PEND ticket and run fetch ----------------------------------------
    t001 = client.post("/tickets", json=tickets["T001"])
    run = t001.json()
    check("T001 PEND", t001.status_code == 200 and run["outcome"] == "PEND", t001.text[:200])
    check(
        "T001 approval fields",
        run["state"] == "AWAITING_APPROVAL"
        and run["human_approval_required"] is True
        and run["approver_role"] == "SUPPORT_LEAD"
        and run["action"]["amount_usd"] == 210.0,
    )
    check(
        "T001 trace fields",
        run["trace"]["classification_path"] == "pattern"
        and run["trace"]["latency_ms"] >= 0
        and run["trace"]["tokens_total"] == 0,
    )
    run_id = run["run_id"]
    fetched = client.get(f"/runs/{run_id}")
    check(
        "GET run waiting",
        fetched.status_code == 200 and fetched.json()["state"] == "AWAITING_APPROVAL",
    )
    check("request headers", bool(t001.headers.get("x-request-id")) and "x-response-time-ms" in t001.headers)

    before = [row for row in client.get("/ledger").json() if row["run_id"] == run_id]
    check("ledger one proposed row", len(before) == 1 and before[0]["status"] == "PROPOSED")

    # 3. Decision idempotency ---------------------------------------------
    body = {"decision": "approve", "approver": "dry-run-lead"}
    approved = client.post(f"/runs/{run_id}/decision", json=body)
    repeated = client.post(f"/runs/{run_id}/decision", json=body)
    conflict = client.post(
        f"/runs/{run_id}/decision", json={"decision": "reject", "approver": "dry-run-other"}
    )
    after = [row for row in client.get("/ledger").json() if row["run_id"] == run_id]
    check(
        "approve state",
        approved.status_code == 200
        and approved.json()["state"] == "APPROVED"
        and approved.json()["human_approval_required"] is False,
    )
    check("repeat idempotent", repeated.status_code == 200 and repeated.json()["state"] == "APPROVED")
    check("conflicting decision 409", conflict.status_code == 409, conflict.text[:160])
    check(
        "ledger updated in place",
        len(after) == 1 and after[0]["status"] == "APPROVED" and after[0]["approver"] == "dry-run-lead",
    )

    # 4. PROCEED and ticket idempotency -----------------------------------
    proceed = client.post("/tickets", json=tickets["T002"]).json()
    repeat_ticket = client.post("/tickets", json=tickets["T002"]).json()
    proceed_rows = [row for row in client.get("/ledger").json() if row["ticket_id"] == "T002"]
    check(
        "T002 PROCEED",
        proceed["outcome"] == "PROCEED"
        and proceed["state"] == "CLOSED"
        and proceed["action"]["amount_usd"] == 129.0,
    )
    check("ticket idempotent run", proceed["run_id"] == repeat_ticket["run_id"])
    check("ticket idempotent ledger", len(proceed_rows) == 1)

    # 5. Injection guardrail ----------------------------------------------
    injection = client.post("/tickets", json=tickets["T020"]).json()
    check(
        "T020 injection escalation",
        injection["outcome"] == "ESCALATE"
        and injection["approver_role"] == "TRUST_SAFETY"
        and injection["action"]["action"] == "ESCALATION"
        and injection["action"]["amount_usd"] is None,
    )

    # 6. Structured logging ------------------------------------------------
    events = [getattr(record, "event", "") for record in capture.records]
    check(
        "json lifecycle logs",
        {"ticket_processed", "decision_recorded", "decision_conflict", "ledger_listed"}
        .issubset(set(events)),
        ",".join(sorted(set(events))),
    )
    check(
        "log has run context",
        any(
            getattr(record, "event", "") == "ticket_processed"
            and getattr(record, "run_id", "") == run_id
            and getattr(record, "outcome", "") == "PEND"
            for record in capture.records
        ),
    )

    # 7. Report ------------------------------------------------------------
    passed = sum(1 for _, ok, _ in checks if ok)
    print("DRY RUN - Larkfield support agent")
    print("-" * 78)
    for name, ok, detail in checks:
        suffix = f"  ({detail})" if detail and not ok else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    print("-" * 78)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

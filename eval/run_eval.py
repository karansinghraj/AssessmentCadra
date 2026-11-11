"""Evaluation runner for the Larkfield support agent.

Usage:
  python -m eval.run_eval --label after
  python -m eval.run_eval --label before --baseline
  python -m eval.run_eval --offline --label after

The runner uses FastAPI's TestClient (the same app the HTTP service exposes),
writes a timestamped JSON result file, and applies the stated pass rule.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "eval" / "cases.json"
RESULTS_DIR = ROOT / "eval" / "results"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _tickets_by_id() -> dict[str, dict]:
    output = {}
    for name in ["tickets/support_inbox_export.json", "inbox/public_tickets.json"]:
        for ticket in _load_json(ROOT / name):
            output[ticket["ticket_id"]] = ticket
    return output


def _compare(expected: dict, actual: dict) -> list[str]:
    failures = []
    if actual.get("outcome") != expected["outcome"]:
        failures.append(f"outcome: expected {expected['outcome']} got {actual.get('outcome')}")
    if bool(actual.get("human_approval_required")) != bool(expected["human_approval_required"]):
        failures.append(
            f"human_approval_required: expected {expected['human_approval_required']} got {actual.get('human_approval_required')}"
        )
    if actual.get("approver_role") != expected["approver_role"]:
        failures.append(f"approver_role: expected {expected['approver_role']} got {actual.get('approver_role')}")
    actual_amount = (actual.get("action") or {}).get("amount_usd")
    expected_amount = expected["amount_usd"]
    if expected_amount is None:
        if actual_amount is not None:
            failures.append(f"amount_usd: expected null got {actual_amount}")
    else:
        if actual_amount is None or abs(float(actual_amount) - float(expected_amount)) > 0.01:
            failures.append(f"amount_usd: expected {expected_amount} got {actual_amount}")
    if set(actual.get("reason_codes") or []) != set(expected["reason_codes"]):
        failures.append(
            f"reason_codes: expected {sorted(expected['reason_codes'])} got {sorted(actual.get('reason_codes') or [])}"
        )
    return failures


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def run(label: str, baseline: bool, offline: bool) -> dict:
    if offline:
        os.environ["LLM_API_KEY"] = ""
    os.environ.setdefault("LOG_LEVEL", "ERROR")
    os.environ.setdefault("RUN_DB_PATH", str(Path(tempfile.mkdtemp(prefix="larkfield_eval_")) / "runs.db"))
    if baseline:
        os.environ["DISABLE_DAMAGE_EXCEPTION"] = "1"
    else:
        os.environ.pop("DISABLE_DAMAGE_EXCEPTION", None)

    # Import after env is set: the app builds its store and config at import.
    from fastapi.testclient import TestClient
    from schema.response_schema import TicketResponse
    from src.app import app

    logging.getLogger("larkfield.api").setLevel(logging.ERROR)
    client = TestClient(app)
    cases_config = _load_json(CASES_PATH)
    tickets = _tickets_by_id()

    health = client.get("/health")
    contract = {
        "health_status": health.status_code,
        "health_body": health.json(),
        "health_exact": health.status_code == 200 and health.json() == {"status": "ok"},
    }

    cases: list[dict] = []
    for ticket_id, expected in cases_config["labelled"].items():
        cases.append({"case_id": ticket_id, "kind": "labelled", "ticket": tickets[ticket_id], "expected": expected})
    for ticket_id, expected in cases_config["public"].items():
        cases.append({"case_id": ticket_id, "kind": "public", "ticket": tickets[ticket_id], "expected": expected})
    for custom in cases_config["custom"]:
        cases.append(
            {
                "case_id": custom["case_id"],
                "kind": custom["kind"],
                "ticket": custom["ticket"],
                "expected": custom["expected"],
            }
        )

    results = []
    for case in cases:
        started = time.perf_counter()
        response = client.post("/tickets", json=case["ticket"])
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            results.append(
                {
                    "case_id": case["case_id"],
                    "kind": case["kind"],
                    "pass": False,
                    "failures": [f"http_status {response.status_code}: {response.text[:300]}"],
                    "expected": case["expected"],
                    "actual": None,
                    "latency_ms": elapsed_ms,
                    "cost_usd": 0.0,
                    "model_calls": [],
                    "classification_path": None,
                }
            )
            continue
        actual = response.json()
        try:
            TicketResponse.model_validate(actual)
            schema_valid = True
        except Exception as exc:
            schema_valid = False
            schema_error = str(exc)[:300]
        failures = _compare(case["expected"], actual)
        if not schema_valid:
            failures.append("schema_validation_failed")
        trace = actual.get("trace") or {}
        results.append(
            {
                "case_id": case["case_id"],
                "kind": case["kind"],
                "pass": not failures,
                "failures": failures,
                "expected": case["expected"],
                "actual": {
                    "ticket_type": actual.get("ticket_type"),
                    "outcome": actual.get("outcome"),
                    "reason_codes": actual.get("reason_codes"),
                    "human_approval_required": actual.get("human_approval_required"),
                    "approver_role": actual.get("approver_role"),
                    "amount_usd": (actual.get("action") or {}).get("amount_usd"),
                    "resolved": actual.get("resolved"),
                    "state": actual.get("state"),
                },
                "latency_ms": trace.get("latency_ms", elapsed_ms),
                "cost_usd": trace.get("cost_usd", 0.0),
                "model_calls": trace.get("model_calls", []),
                "classification_path": trace.get("classification_path"),
                "sanity_issues": trace.get("sanity_issues", []),
            }
        )

    # ---- contract / idempotency checks (run after the scored cases) -----
    pend = next((r for r in results if r["pass"] and r["actual"]["outcome"] == "PEND"), None)
    if pend:
        run_id = None
        # Find the run id by posting the same ticket again (idempotent).
        ticket = next(case["ticket"] for case in cases if case["case_id"] == pend["case_id"])
        run_id = client.post("/tickets", json=ticket).json()["run_id"]
        before_ledger = client.get("/ledger").json()
        first = client.post(f"/runs/{run_id}/decision", json={"decision": "approve", "approver": "eval-lead"})
        second = client.post(f"/runs/{run_id}/decision", json={"decision": "approve", "approver": "eval-lead"})
        after_ledger = client.get("/ledger").json()
        reject = client.post(f"/runs/{run_id}/decision", json={"decision": "reject", "approver": "other"})
        run_rows = [row for row in before_ledger if row["run_id"] == run_id]
        after_rows = [row for row in after_ledger if row["run_id"] == run_id]
        contract.update(
            {
                "pending_decision_status": first.status_code,
                "pending_decision_state": (first.json() or {}).get("state"),
                "repeat_decision_status": second.status_code,
                "conflicting_decision_status": reject.status_code,
                "ledger_rows_for_run_before": len(run_rows),
                "ledger_rows_for_run_after": len(after_rows),
                "ledger_approved_once": len(after_rows) == 1 and after_rows[0]["status"] == "APPROVED",
            }
        )
    else:
        contract["pending_decision_status"] = None

    repeated_ticket = next(case for case in cases if case["case_id"] == "T002")
    first_post = client.post("/tickets", json=repeated_ticket["ticket"]).json()
    second_post = client.post("/tickets", json=repeated_ticket["ticket"]).json()
    t002_rows = [row for row in client.get("/ledger").json() if row["ticket_id"] == "T002"]
    contract.update(
        {
            "ticket_idempotent_same_run": first_post.get("run_id") == second_post.get("run_id"),
            "ticket_idempotent_one_ledger_row": len(t002_rows) == 1,
        }
    )

    passed = sum(1 for result in results if result["pass"])
    latencies = [float(result["latency_ms"]) for result in results]
    costs = [float(result["cost_usd"]) for result in results]
    summary = {
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "median_latency_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": round(_p95(latencies), 1),
        "median_cost_usd": round(statistics.median(costs), 8) if costs else 0.0,
        "p95_cost_usd": round(_p95(costs), 8),
        "total_cost_usd": round(sum(costs), 8),
        "total_input_tokens": sum(sum(call.get("input_tokens", 0) for call in result["model_calls"]) for result in results),
        "total_output_tokens": sum(sum(call.get("output_tokens", 0) for call in result["model_calls"]) for result in results),
        "model_ids": sorted({call.get("model") for result in results for call in result["model_calls"]}),
    }
    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "suite": "offline" if offline else "configured-provider",
        "baseline_without_damage_exception": baseline,
        "pass_rule": cases_config["pass_rule"],
        "summary": summary,
        "contract": contract,
        "failures": [result["case_id"] for result in results if not result["pass"]],
        "cases": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{stamp}_{label}.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    output["result_file"] = str(path.relative_to(ROOT))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Larkfield evaluation suite.")
    parser.add_argument("--label", default="run", help="result file suffix, e.g. before/after")
    parser.add_argument("--baseline", action="store_true", help="disable the 45-day photo exception for a before run")
    parser.add_argument("--offline", action="store_true", help="force rule-only mode (no model key)")
    args = parser.parse_args(argv)
    result = run(args.label, args.baseline, args.offline)
    print(json.dumps({k: result[k] for k in ["result_file", "summary", "contract", "failures"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

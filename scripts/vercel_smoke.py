"""Serverless smoke test for the Vercel-style entrypoint.

Run:  python -m scripts.vercel_smoke

Simulates `VERCEL=1`, imports `main:app`, and proves that the SQLite database
and prepared-data cache are written under the platform temp directory instead
of the read-only repository. Uses no model key.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    os.environ["VERCEL"] = "1"
    os.environ["LLM_API_KEY"] = ""
    os.environ["LOG_LEVEL"] = "ERROR"
    for key in ("RUN_DB_PATH", "DATA_BUILD_DIR", "DATA_DIR", "SERVERLESS"):
        os.environ.pop(key, None)

    from fastapi.testclient import TestClient

    from src.config import Config

    # 1. Prove the real serverless defaults land under the platform temp dir.
    defaults = Config.from_env()
    temp = Path(tempfile.gettempdir()).resolve()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    check("serverless detected", defaults.serverless)
    check("db under temp", defaults.run_db_path.resolve().parent == temp, str(defaults.run_db_path))
    check("build under temp", defaults.build_dir.resolve().parent == temp, str(defaults.build_dir))

    # 2. Use a fresh temp directory for the functional part so reruns are
    #    repeatable. /tmp survives warm invocations, so the default DB may
    #    already hold a decided T001 from an earlier smoke run.
    runtime_dir = Path(tempfile.mkdtemp(prefix="larkfield_vercel_"))
    os.environ["RUN_DB_PATH"] = str(runtime_dir / "runs.db")
    os.environ["DATA_BUILD_DIR"] = str(runtime_dir / "build")

    from src.config import get_config

    cfg = get_config()
    from main import app  # noqa: E402
    check("runtime paths under temp", cfg.run_db_path.resolve().parent == runtime_dir)

    client = TestClient(app)
    health = client.get("/health")
    check("health exact", health.status_code == 200 and health.json() == {"status": "ok"})

    schema = client.get("/openapi.json")
    check("openapi", schema.status_code == 200 and "/tickets" in schema.json()["paths"])

    rows = json.loads((ROOT / "tickets" / "support_inbox_export.json").read_text(encoding="utf-8"))
    ticket = next(row for row in rows if row["ticket_id"] == "T001")
    run = client.post("/tickets", json=ticket).json()
    check(
        "PEND ticket",
        run["outcome"] == "PEND"
        and run["state"] == "AWAITING_APPROVAL"
        and run["action"]["amount_usd"] == 210.0,
    )

    repeated = client.post("/tickets", json=ticket).json()
    check("ticket idempotent", repeated["run_id"] == run["run_id"])

    approved = client.post(
        f"/runs/{run['run_id']}/decision",
        json={"decision": "approve", "approver": "vercel-smoke-lead"},
    )
    ledger = [row for row in client.get("/ledger").json() if row["run_id"] == run["run_id"]]
    check("approve", approved.status_code == 200 and approved.json()["state"] == "APPROVED")
    check(
        "ledger one row",
        len(ledger) == 1 and ledger[0]["status"] == "APPROVED",
    )

    passed = sum(1 for _, ok, _ in checks if ok)
    print("VERCEL SERVERLESS SMOKE")
    print("-" * 78)
    print(f"default db={defaults.run_db_path}")
    print(f"default build={defaults.build_dir}")
    print(f"runtime db={cfg.run_db_path}")
    print(f"runtime build={cfg.build_dir}")
    for name, ok, detail in checks:
        suffix = f"  ({detail})" if detail and not ok else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    print("-" * 78)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

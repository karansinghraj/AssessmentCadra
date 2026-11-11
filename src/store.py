"""SQLite persistence for runs, decisions and the action ledger.

Idempotency is enforced by unique keys, not by application memory:
  * one run per ticket_id
  * one decision per run (first decision wins)
  * one ledger row per (run_id, action)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .normalize import iso_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    ticket_id TEXT UNIQUE NOT NULL,
    ticket_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    state TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(run_id)
);
CREATE TABLE IF NOT EXISTS ledger (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    ticket_id TEXT NOT NULL,
    action TEXT NOT NULL,
    amount_usd REAL,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    approver TEXT,
    decided_at TEXT,
    PRIMARY KEY (run_id, action)
);
CREATE INDEX IF NOT EXISTS idx_ledger_recorded_at ON ledger(recorded_at);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- runs ----------------------------------------------------------
    def create_run(self, run_id: str, ticket_id: str, ticket: dict, response: dict, state: str) -> dict:
        now = iso_now()
        payload = json.dumps(ticket, ensure_ascii=False, sort_keys=True)
        response_payload = json.dumps(response, ensure_ascii=False, sort_keys=True)
        with self._lock:
            existing = self._conn.execute(
                "SELECT response_json FROM runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if existing:
                return json.loads(existing["response_json"])
            self._conn.execute(
                "INSERT INTO runs (run_id, ticket_id, ticket_json, response_json, state, outcome, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, ticket_id, payload, response_payload, state, response["outcome"], now, now),
            )
            action = response.get("action") or {}
            self._conn.execute(
                "INSERT INTO ledger (run_id, ticket_id, action, amount_usd, status, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    ticket_id,
                    action.get("action") or "REPLY",
                    action.get("amount_usd"),
                    action.get("status") or "PROPOSED",
                    now,
                ),
            )
            self._conn.commit()
        return response

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        response = json.loads(row["response_json"])
        response["state"] = row["state"]
        response["decisions"] = self.get_decisions(run_id)
        return response

    def get_run_by_ticket(self, ticket_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not row:
            return None
        response = json.loads(row["response_json"])
        response["state"] = row["state"]
        response["decisions"] = self.get_decisions(row["run_id"])
        return response

    def save_response(self, run_id: str, response: dict, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET response_json = ?, state = ?, outcome = ?, updated_at = ? WHERE run_id = ?",
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    state,
                    response["outcome"],
                    iso_now(),
                    run_id,
                ),
            )
            self._conn.commit()

    # ---- decisions -----------------------------------------------------
    def get_decisions(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision, approver, decided_at FROM decisions WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_decision(self, run_id: str, decision: str, approver: str) -> tuple[bool, dict | None]:
        """Return (inserted, existing). First decision wins; repeats are idempotent."""
        now = iso_now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT decision, approver, decided_at FROM decisions WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing:
                return False, dict(existing)
            self._conn.execute(
                "INSERT INTO decisions (run_id, decision, approver, decided_at) VALUES (?, ?, ?, ?)",
                (run_id, decision, approver, now),
            )
            self._conn.commit()
        return True, {"decision": decision, "approver": approver, "decided_at": now}

    def update_ledger(self, run_id: str, action: str, status: str, approver: str | None, decided_at: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ledger SET status = ?, approver = ?, decided_at = ? WHERE run_id = ? AND action = ?",
                (status, approver, decided_at, run_id, action),
            )
            self._conn.commit()

    # ---- ledger --------------------------------------------------------
    def list_ledger(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, ticket_id, action, amount_usd, status, recorded_at, approver, decided_at "
                "FROM ledger ORDER BY recorded_at, run_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def approved_refunds_for_order(self, order_id: str) -> dict[tuple[str, int], int]:
        """Approved refund proposals, used to prevent a forwarded duplicate paying twice."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.response_json FROM ledger l JOIN runs r ON r.run_id = l.run_id "
                "WHERE l.action = 'REFUND' AND l.status = 'APPROVED'"
            ).fetchall()
        output: dict[tuple[str, int], int] = {}
        for row in rows:
            try:
                response = json.loads(row["response_json"])
            except json.JSONDecodeError:
                continue
            if (response.get("resolved") or {}).get("order_id") != order_id:
                continue
            action = response.get("action") or {}
            line_no = action.get("line_no")
            amount = action.get("amount_usd")
            if line_no is None or amount is None:
                continue
            key = (order_id, int(line_no))
            output[key] = output.get(key, 0) + int(round(float(amount) * 100))
        return output

    def close(self) -> None:
        with self._lock:
            self._conn.close()

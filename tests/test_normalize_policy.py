"""Normalisation and policy unit tests."""
from __future__ import annotations

import json
from pathlib import Path

from src.guardrails import analyse
from src.normalize import extract_order_refs, parse_date, parse_money_cents
from src.policy import decide
from src.prepare_data import build_data

ROOT = Path(__file__).resolve().parent.parent


def _tickets() -> dict[str, dict]:
    data = json.loads((ROOT / "tickets" / "support_inbox_export.json").read_text(encoding="utf-8"))
    return {ticket["ticket_id"]: ticket for ticket in data}


def test_order_reference_variants():
    text = "LF-100200 LF100201 #100202 order 100203 order order 100204."
    assert extract_order_refs(text) == ["LF-100200", "LF-100201", "LF-100202", "LF-100203", "LF-100204"]


def test_order_reference_trailing_period():
    assert extract_order_refs("Please refund order #100230.") == ["LF-100230"]


def test_slash_dates_are_day_first():
    assert parse_date("26/08/2026").isoformat() == "2026-08-26"
    assert parse_date("08/10/2026").isoformat() == "2026-10-08"


def test_money_variants():
    assert parse_money_cents("$129.00") == 12900
    assert parse_money_cents("USD 24.00") == 2400
    assert parse_money_cents("186.50") == 18650


def test_decision_matrix_labelled_tickets():
    data = build_data(ROOT / "data")
    expected = {
        "T001": ("PEND", 21000, {"DAMAGE_EXCEPTION", "APPROVAL_REQUIRED"}),
        "T019": ("PROCEED_MODIFIED", 2750, {"PARTIAL_LINES"}),
        "T026": ("PEND", 18500, {"ALREADY_REFUNDED", "APPROVAL_REQUIRED"}),
        "T011": ("ESCALATE", None, {"LOST_IN_TRANSIT"}),
        "T014": ("REJECT", 0, {"OUT_OF_WINDOW"}),
        "T018": ("REJECT", 0, {"ALREADY_REFUNDED"}),
        "T013": ("UNABLE_TO_VERIFY", None, {"CARRIER_STATUS_UNAVAILABLE"}),
    }
    tickets = _tickets()
    for ticket_id, (outcome, amount, reasons) in expected.items():
        result = decide(tickets[ticket_id], data, analyse(tickets[ticket_id]))
        assert result["outcome"] == outcome, ticket_id
        assert set(result["reason_codes"]) == reasons, ticket_id
        actual_amount = None if result["action"]["amount_usd"] is None else int(round(result["action"]["amount_usd"] * 100))
        assert actual_amount == amount, ticket_id


def test_above_limit_always_pends():
    data = build_data(ROOT / "data")
    ticket = _tickets()["T003"]
    result = decide(ticket, data, analyse(ticket))
    assert result["outcome"] == "PEND"
    assert result["approver_role"] == "SUPPORT_LEAD"
    assert result["human_approval_required"] is True

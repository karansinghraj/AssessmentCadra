"""Verify the optional model path records model id, tokens and cost."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src import extract
from src.config import get_config

ROOT = Path(__file__).resolve().parent.parent


def test_llm_calls_are_traced(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    cfg = get_config()

    def fake_chat(_cfg, messages, max_tokens=0, **_kwargs):
        prompt = messages[-1]["content"]
        if "extraction function" in messages[0]["content"] or "Schema:" in prompt:
            content = (
                '{"ticket_type":"REFUND","order_refs":[],"product":"Arc floor lamp",'
                '"damage":true,"wrong_item":false,"non_delivery":false,"stuck":false,'
                '"status_request":false,"return_request":false,"refund_request":true,'
                '"whole_order":false,"shipping_request":false,"injection":false,'
                '"affected_qty":1,"confidence":0.8}'
            )
        else:
            content = '{"verdict":"ok","hard_safety_issue":false,"issues":[]}'
        call = extract.ModelCall(
            model="fake-model",
            input_tokens=900,
            output_tokens=100,
            cost_usd=cfg.cost_usd(900, 100),
            latency_ms=5,
            ok=True,
        )
        return content, call

    monkeypatch.setattr(extract, "_chat", fake_chat)
    from src.app import create_app

    client = TestClient(create_app())
    ticket = {
        "ticket_id": "LLM1",
        "received_at": "2026-10-01T10:00:00+00:00",
        "channel": "email",
        "from": "ben.tanaka1@mail.example",
        "to": "support@larkfield.example",
        "subject": "Broken lamp",
        "body": "My Arc floor lamp is broken, please refund.",
        "attachments": [],
    }
    body = client.post("/tickets", json=ticket).json()
    calls = body["trace"]["model_calls"]
    assert body["trace"]["tokens_total"] > 0
    assert body["trace"]["cost_usd"] > 0
    assert all(call["model"] == "fake-model" for call in calls)
    assert len(calls) == 2  # classifier + judge
    assert body["outcome"] == "INVALID_REQUEST"

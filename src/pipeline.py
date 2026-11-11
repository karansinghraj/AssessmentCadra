"""Request pipeline: pattern route -> optional LLM -> policy -> sanity/judge.

This is the only place that assembles a response. The model is optional at every
stage; the deterministic path always produces a safe decision.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import replace

from . import extract, guardrails, policy
from .config import Config
from .normalize import iso_now
from .replies import generic_fallback
from .store import Store


def _merge(pattern, classifier_data: dict | None):
    if not classifier_data:
        return pattern
    merged = replace(pattern)
    merged.order_refs = pattern.order_refs or list(classifier_data.get("order_refs") or [])
    if merged.ticket_type == "OTHER" and classifier_data.get("ticket_type"):
        merged.ticket_type = classifier_data["ticket_type"]
    for key in [
        "damage",
        "wrong_item",
        "non_delivery",
        "stuck",
        "status_request",
        "return_request",
        "refund_request",
        "whole_order",
        "shipping_request",
        "injection",
    ]:
        setattr(merged, key, bool(getattr(pattern, key) or classifier_data.get(key)))
    if classifier_data.get("product") and not merged.product_hint:
        merged.product_hint = str(classifier_data["product"])
    if merged.affected_qty is None and classifier_data.get("affected_qty") is not None:
        merged.affected_qty = classifier_data["affected_qty"]
    if merged.injection and not merged.injection_matches:
        merged.injection_matches = ["llm_classifier"]
    return merged


def _fail_closed(ticket: dict, response: dict, issues: list[str], source: str) -> dict:
    response["outcome"] = "UNABLE_TO_VERIFY"
    response["reason_codes"] = []
    response["human_approval_required"] = False
    response["approver_role"] = None
    response["action"] = {
        "action": "REPLY",
        "amount_usd": None,
        "line_no": None,
        "item": None,
        "status": "APPROVED",
    }
    response["draft_reply"] = generic_fallback(ticket, (response.get("resolved") or {}).get("order_id"))
    response["checks"].append(
        policy.check(
            "OUTPUT_SANITY",
            "FAIL",
            "internal safety gate",
            [policy.evidence(source, "sanity", "; ".join(issues)[:500])],
        )
    )
    response["state"] = "CLOSED"
    return response


def process_ticket(ticket: dict, data: dict, store: Store, cfg: Config) -> dict:
    started = time.perf_counter()
    ticket = dict(ticket)
    ticket["from_"] = ticket.get("from_") or ticket.get("from") or ""
    run_id = f"run_{uuid.uuid4().hex[:20]}"

    analysis = guardrails.analyse(ticket)
    model_calls: list[extract.ModelCall] = []
    tools = ["pattern_router"]

    classifier_used = False
    if cfg.llm_configured and cfg.llm_classifier_enabled and analysis.route == "llm":
        classifier = extract.classify(ticket, analysis, cfg)
        if classifier is not None:
            classifier_used = True
            tools.append("llm_classify")
            model_calls.append(classifier.call)
            analysis = _merge(analysis, classifier.data)

    local_refunds: dict[tuple[str, int], int] = {}
    if len(analysis.order_refs) == 1:
        local_refunds = store.approved_refunds_for_order(analysis.order_refs[0])

    decision = policy.decide(ticket, data, analysis, local_refunds=local_refunds)
    response = {
        "ticket_id": ticket.get("ticket_id"),
        "run_id": run_id,
        "ticket_type": decision["ticket_type"],
        "resolved": decision["resolved"],
        "outcome": decision["outcome"],
        "reason_codes": decision["reason_codes"],
        "checks": decision["checks"],
        "human_approval_required": decision["human_approval_required"],
        "approver_role": decision["approver_role"],
        "draft_reply": decision["draft_reply"],
        "action": decision["action"],
        "state": "CLOSED",
        "decisions": [],
    }
    if response["outcome"] == "PEND":
        response["state"] = "AWAITING_APPROVAL"
    elif response["outcome"] == "ESCALATE":
        response["state"] = "ESCALATED"

    # Deterministic final sanity gate. Failure means fail closed, never a refund.
    sanity_issues = guardrails.sanity_check(response)
    if sanity_issues:
        response = _fail_closed(ticket, response, sanity_issues, "sanity_check")

    # Optional final LLM judge. It can only block/fallback; it cannot rewrite money.
    judge_info = None
    if cfg.llm_configured and cfg.llm_judge_enabled:
        judged = extract.judge(ticket, response, analysis, cfg)
        if judged is not None:
            tools.append("llm_judge")
            model_calls.append(judged.call)
            judge_info = {
                "verdict": judged.verdict,
                "hard_safety_issue": judged.hard_safety_issue,
                "issues": judged.issues,
                "latency_ms": judged.call.latency_ms,
                "ok": judged.call.ok,
            }
            if judged.hard_safety_issue:
                response = _fail_closed(
                    ticket,
                    response,
                    ["llm_judge: " + "; ".join(judged.issues)[:400]],
                    "llm_judge",
                )

    if not model_calls:
        model_calls.append(
            extract.ModelCall(
                model="rule-based-fallback",
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                latency_ms=0,
                ok=False,
                error="llm_not_configured_or_not_needed",
            )
        )

    total_input = sum(call.input_tokens for call in model_calls)
    total_output = sum(call.output_tokens for call in model_calls)
    total_cost = sum(call.cost_usd for call in model_calls)
    latency_ms = int((time.perf_counter() - started) * 1000)
    if classifier_used:
        classification_path = "llm"
    elif analysis.route == "guardrail":
        classification_path = "pattern_guardrail"
    elif analysis.route == "llm":
        classification_path = "pattern_fallback"
    else:
        classification_path = "pattern"

    classification_route = analysis.route
    response["trace"] = {
        "model_calls": [call.to_trace() for call in model_calls],
        "tools_called": tools,
        "documents_used": [
            {"document": "policies/refund_policy_RP-3.2.md", "version": "3.2"},
            {"document": "policies/help_centre_faq_v2.md", "version": "2 (superseded, precedence only)"},
        ],
        "cost_usd": round(total_cost, 8),
        "latency_ms": latency_ms,
        "tokens_total": total_input + total_output,
        "pricing": {
            "input_usd_per_mtok": cfg.llm_input_per_mtok,
            "output_usd_per_mtok": cfg.llm_output_per_mtok,
            "model": cfg.llm_model,
        },
        "classification_path": classification_path,
        "classification_route": classification_route,
        "classifier_used": classifier_used,
        "sanity_issues": sanity_issues,
        "judge": judge_info,
        "calls_detail": [call.to_extra() for call in model_calls],
        "processed_at": iso_now(),
    }
    return response

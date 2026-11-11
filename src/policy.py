"""Deterministic policy engine.

Every outcome is derived from checks over prepared data. The LLM is not used
here for money, eligibility or authority decisions.
"""
from __future__ import annotations

import os
import re
from datetime import date

from .guardrails import PatternAnalysis
from .normalize import (
    days_between,
    format_usd,
    normalize_email,
    parse_date,
    parse_datetime,
)
from .replies import compose

POLICY_DOC = "policies/refund_policy_RP-3.2.md"
POLICY_VERSION = "3.2"
AUTO_LIMIT_CENTS = 15000


def evidence(source: str, ref: str, value: object) -> dict:
    return {"source": source, "ref": str(ref), "value": str(value)}


def check(check_id: str, result: str, clause: str, items: list[dict] | None = None) -> dict:
    return {
        "check_id": check_id,
        "result": result,
        "evidence": items or [],
        "policy_clause": clause,
    }


def _photo_present(ticket: dict) -> bool:
    for attachment in ticket.get("attachments") or []:
        content_type = str(attachment.get("content_type") or "").lower()
        file_name = str(attachment.get("file_name") or "").lower()
        if content_type.startswith("image/") or re.search(r"\.(jpg|jpeg|png|webp|gif|heic)$", file_name):
            return True
    return False


STOPWORDS = {
    "the",
    "a",
    "an",
    "my",
    "from",
    "order",
    "please",
    "this",
    "that",
    "item",
    "with",
    "and",
    "for",
    "your",
    "our",
    "was",
    "were",
    "is",
    "are",
    "it",
    "on",
    "of",
    "to",
    "in",
}


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", (value or "").lower()) if token not in STOPWORDS}


def _match_line(order: dict, text: str, product_hint: str | None) -> dict | None:
    lines = order.get("lines") or []
    if not lines:
        return None
    text_tokens = _tokens(text)
    hint_tokens = _tokens(product_hint or "")
    scored: list[tuple[int, dict]] = []
    for line in lines:
        name = line.get("product_name") or ""
        name_lower = name.lower()
        name_tokens = _tokens(name)
        score = 0
        if name_lower and name_lower in text:
            score += 10
        score += len(name_tokens & text_tokens)
        if hint_tokens:
            score += 2 * len(name_tokens & hint_tokens)
            hint_lower = (product_hint or "").lower()
            if name_lower and (name_lower in hint_lower or any(token in hint_lower for token in name_tokens)):
                score += 4
        scored.append((score, line))
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score = scored[0][0]
    if best_score == 0:
        return lines[0] if len(lines) == 1 else None
    if len(scored) > 1 and scored[1][0] == best_score:
        return None
    return scored[0][1]


def _affected_qty(analysis: PatternAnalysis, line: dict, text: str) -> int:
    ordered = int(line.get("qty") or 1)
    if analysis.affected_qty == -1:
        return ordered
    if analysis.affected_qty and analysis.affected_qty > 0:
        return min(int(analysis.affected_qty), ordered)
    # "two chairs", "2 chairs" with the product name immediately after.
    first_token = next(iter(_tokens(line.get("product_name") or "")), None)
    if first_token:
        match = re.search(
            rf"\b(one|two|three|four|1|2|3|4)\s+(?:of\s+the\s+)?(?:[a-z]+\s+){{0,3}}{re.escape(first_token)}",
            text,
            re.IGNORECASE,
        )
        if match:
            word = match.group(1).lower()
            return min({"one": 1, "two": 2, "three": 3, "four": 4}.get(word, int(word) if word.isdigit() else 1), ordered)
    return min(1, ordered)


def _base_checks(customer: dict | None, sender: str) -> list[dict]:
    return [
        check(
            "POLICY_PRECEDENCE",
            "PASS",
            "RP-3.2 header / supersedes earlier guidance",
            [
                evidence(POLICY_DOC, "header", "Version 3.2 effective 2026-07-01; replaces RP-3.1 and earlier guidance"),
                evidence("policies/help_centre_faq_v2.md", "header", "Version 2 published 2025-10-14; superseded, not used"),
            ],
        ),
        check(
            "CUSTOMER_IDENTITY",
            "PASS" if customer else "FAIL",
            "RP-3.2 §4.1",
            [
                evidence(
                    "data/customers.csv",
                    customer["customer_id"] if customer else sender or "(empty)",
                    "email on file" if customer else "sender email not on file",
                )
            ],
        ),
    ]


def decide(
    ticket: dict,
    data: dict,
    analysis: PatternAnalysis,
    local_refunds: dict[tuple[str, int], int] | None = None,
    baseline_without_damage_exception: bool | None = None,
) -> dict:
    local_refunds = local_refunds or {}
    if baseline_without_damage_exception is None:
        baseline_without_damage_exception = os.getenv("DISABLE_DAMAGE_EXCEPTION", "") == "1"

    sender = normalize_email(ticket.get("from_") or ticket.get("from"))
    customer_id = data.get("customers_by_email", {}).get(sender)
    customer = data.get("customers", {}).get(customer_id) if customer_id else None
    refs = list(analysis.order_refs)
    requested_ref = refs[0] if len(refs) == 1 else None
    order = data.get("orders", {}).get(requested_ref) if requested_ref else None
    shipment = data.get("shipments", {}).get(requested_ref) if requested_ref else None
    text = f"{ticket.get('subject', '')} {ticket.get('body', '')}".lower()
    has_photo = _photo_present(ticket)

    resolved = {
        "order_id": requested_ref if order else None,
        "customer_id": (order or {}).get("customer_id") if order else (customer or {}).get("customer_id"),
    }
    checks = _base_checks(customer, sender)

    # Injection is resolved first, but the order facts are still reported.
    checks.append(
        check(
            "TICKET_INJECTION",
            "FAIL" if analysis.injection else "PASS",
            "RP-3.2 §5.2",
            [
                evidence(
                    "ticket",
                    ticket.get("ticket_id", ""),
                    "; ".join(analysis.injection_matches[:4]) if analysis.injection else "no override attempt",
                )
            ],
        )
    )
    if refs:
        checks.append(
            check(
                "ORDER_EXISTS",
                "PASS" if order else "FAIL",
                "RP-3.2 §4.1",
                [evidence("data/orders.csv", requested_ref or refs, "found" if order else "not found")],
            )
        )
        if order:
            checks.append(
                check(
                    "ORDER_OWNERSHIP",
                    "PASS" if customer and order.get("customer_id") == customer.get("customer_id") else "FAIL",
                    "RP-3.2 §4.1",
                    [
                        evidence(
                            "data/orders.csv",
                            order.get("order_id", ""),
                            f"order customer={order.get('customer_id')} sender={customer.get('customer_id') if customer else 'unknown'}",
                        )
                    ],
                )
            )

    if analysis.injection:
        reply = compose(
            ticket,
            {
                "outcome": "ESCALATE",
                "approver_role": "TRUST_SAFETY",
                "order_id": resolved.get("order_id"),
                "reason_codes": ["POLICY_OVERRIDE_ATTEMPT"],
            },
        )
        return _result(
            analysis,
            resolved,
            "ESCALATE",
            ["POLICY_OVERRIDE_ATTEMPT"],
            checks,
            True,
            "TRUST_SAFETY",
            {"action": "ESCALATION", "amount_usd": None, "line_no": None, "item": None, "status": "PROPOSED"},
            reply,
            {"order_id": resolved.get("order_id"), "requested_ref": requested_ref},
        )

    # Non-actionable messages get a generic support reply, not an order lookup.
    if analysis.ticket_type == "OTHER":
        reply = compose(ticket, {"outcome": "PROCEED", "ticket_type": "OTHER", "order_id": resolved.get("order_id")})
        return _result(
            analysis,
            resolved,
            "PROCEED",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": resolved.get("order_id"), "requested_ref": requested_ref},
        )

    if not customer:
        reply = compose(
            ticket,
            {
                "outcome": "INVALID_REQUEST",
                "reason_codes": ["CUSTOMER_MISMATCH"],
                "order_id": resolved.get("order_id"),
                "requested_ref": requested_ref,
            },
        )
        return _result(
            analysis,
            resolved,
            "INVALID_REQUEST",
            ["CUSTOMER_MISMATCH"],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": resolved.get("order_id"), "requested_ref": requested_ref},
        )

    if not refs:
        reply = compose(
            ticket,
            {"outcome": "INVALID_REQUEST", "reason_codes": ["ORDER_NOT_FOUND"], "requested_ref": None},
        )
        return _result(
            analysis,
            resolved,
            "INVALID_REQUEST",
            ["ORDER_NOT_FOUND"],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": None, "requested_ref": None},
        )

    if len(refs) > 1:
        checks.append(
            check(
                "ORDER_EXISTS",
                "UNABLE_TO_VERIFY",
                "RP-3.2 §4.1",
                [evidence("data/orders.csv", ", ".join(refs), "multiple distinct order references")],
            )
        )
        reply = compose(ticket, {"outcome": "UNABLE_TO_VERIFY", "order_id": None})
        return _result(
            analysis,
            {"order_id": None, "customer_id": customer.get("customer_id")},
            "UNABLE_TO_VERIFY",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": None, "requested_ref": None},
        )

    if not order:
        reply = compose(
            ticket,
            {"outcome": "INVALID_REQUEST", "reason_codes": ["ORDER_NOT_FOUND"], "requested_ref": requested_ref},
        )
        return _result(
            analysis,
            {"order_id": None, "customer_id": customer.get("customer_id")},
            "INVALID_REQUEST",
            ["ORDER_NOT_FOUND"],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": None, "requested_ref": requested_ref},
        )

    if order.get("customer_id") != customer.get("customer_id"):
        reply = compose(
            ticket,
            {
                "outcome": "INVALID_REQUEST",
                "reason_codes": ["CUSTOMER_MISMATCH"],
                "order_id": order.get("order_id"),
                "requested_ref": requested_ref,
            },
        )
        return _result(
            analysis,
            resolved,
            "INVALID_REQUEST",
            ["CUSTOMER_MISMATCH"],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    if order.get("quarantined"):
        checks.append(check("ORDER_EXISTS", "UNABLE_TO_VERIFY", "RP-3.2 §4.1", [evidence("data/orders.csv", order.get("order_id"), "conflicting duplicate rows")]))
        reply = compose(ticket, {"outcome": "UNABLE_TO_VERIFY", "order_id": order.get("order_id")})
        return _result(
            analysis,
            resolved,
            "UNABLE_TO_VERIFY",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    received_date = parse_date((ticket.get("received_at") or "")[:10]) or date.today()
    carrier_unavailable = shipment is None or (shipment.get("status") or "").upper() == "UNAVAILABLE"

    # ---- order status / delivery issue ---------------------------------
    if analysis.ticket_type in {"ORDER_STATUS", "DELIVERY_ISSUE"}:
        if carrier_unavailable:
            checks.append(
                check(
                    "CARRIER_STATUS",
                    "UNABLE_TO_VERIFY",
                    "RP-3.2 §4.2",
                    [evidence("data/shipments.csv", order.get("order_id"), "missing or UNAVAILABLE")],
                )
            )
            reply = compose(
                ticket,
                {
                    "outcome": "UNABLE_TO_VERIFY",
                    "reason_codes": ["CARRIER_STATUS_UNAVAILABLE"],
                    "order_id": order.get("order_id"),
                },
            )
            return _result(
                analysis,
                resolved,
                "UNABLE_TO_VERIFY",
                ["CARRIER_STATUS_UNAVAILABLE"],
                checks,
                False,
                None,
                {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
                reply,
                {"order_id": order.get("order_id"), "requested_ref": requested_ref},
            )

        status = (shipment.get("status") or "").upper()
        delivered_at = shipment.get("delivered_at")
        if status == "DELIVERED":
            checks.append(
                check(
                    "DELIVERY_SCAN",
                    "PASS",
                    "RP-3.2 §5.3",
                    [evidence("data/shipments.csv", shipment.get("shipment_id"), f"DELIVERED {delivered_at}")],
                )
            )
            if analysis.non_delivery:
                reply = compose(
                    ticket,
                    {
                        "outcome": "UNABLE_TO_VERIFY",
                        "order_id": order.get("order_id"),
                        "delivered_at": (delivered_at or "")[:10],
                        "carrier": shipment.get("carrier"),
                        "tracking_no": shipment.get("tracking_no"),
                        "delivered_dispute": True,
                    },
                )
                return _result(
                    analysis,
                    resolved,
                    "UNABLE_TO_VERIFY",
                    [],
                    checks,
                    False,
                    None,
                    {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
                    reply,
                    {"order_id": order.get("order_id"), "requested_ref": requested_ref},
                )
            reply = compose(
                ticket,
                {
                    "outcome": "PROCEED",
                    "ticket_type": analysis.ticket_type,
                    "order_id": order.get("order_id"),
                    "shipment_status": status,
                    "delivered_at": (delivered_at or "")[:10],
                    "carrier": shipment.get("carrier"),
                    "tracking_no": shipment.get("tracking_no"),
                },
            )
            return _result(
                analysis,
                resolved,
                "PROCEED",
                [],
                checks,
                False,
                None,
                {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
                reply,
                {"order_id": order.get("order_id"), "requested_ref": requested_ref},
            )

        last_event = shipment.get("last_event_at")
        idle_days = days_between(received_date, last_event)
        checks.append(
            check(
                "CARRIER_MOVEMENT",
                "FAIL" if idle_days is not None and idle_days > 10 else "PASS",
                "RP-3.2 §5.1",
                [
                    evidence(
                        "data/shipments.csv",
                        shipment.get("shipment_id"),
                        f"status={status} last_event={last_event} idle_days={idle_days}",
                    )
                ],
            )
        )
        if idle_days is None:
            reply = compose(
                ticket,
                {
                    "outcome": "UNABLE_TO_VERIFY",
                    "reason_codes": ["CARRIER_STATUS_UNAVAILABLE"],
                    "order_id": order.get("order_id"),
                },
            )
            return _result(
                analysis,
                resolved,
                "UNABLE_TO_VERIFY",
                ["CARRIER_STATUS_UNAVAILABLE"],
                checks,
                False,
                None,
                {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
                reply,
                {"order_id": order.get("order_id"), "requested_ref": requested_ref},
            )
        if idle_days > 10:
            reply = compose(
                ticket,
                {
                    "outcome": "ESCALATE",
                    "approver_role": "CARRIER_CLAIMS",
                    "order_id": order.get("order_id"),
                    "days": idle_days,
                    "last_event_at": last_event,
                    "reason_codes": ["LOST_IN_TRANSIT"],
                },
            )
            return _result(
                analysis,
                resolved,
                "ESCALATE",
                ["LOST_IN_TRANSIT"],
                checks,
                True,
                "CARRIER_CLAIMS",
                {"action": "ESCALATION", "amount_usd": None, "line_no": None, "item": None, "status": "PROPOSED"},
                reply,
                {"order_id": order.get("order_id"), "requested_ref": requested_ref, "idle_days": idle_days},
            )
        reply = compose(
            ticket,
            {
                "outcome": "PROCEED",
                "ticket_type": analysis.ticket_type,
                "order_id": order.get("order_id"),
                "shipment_status": status,
                "last_event_at": last_event,
                "carrier": shipment.get("carrier"),
                "tracking_no": shipment.get("tracking_no"),
            },
        )
        return _result(
            analysis,
            resolved,
            "PROCEED",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    # ---- refund / return ------------------------------------------------
    if carrier_unavailable:
        checks.append(
            check(
                "CARRIER_STATUS",
                "UNABLE_TO_VERIFY",
                "RP-3.2 §4.2",
                [evidence("data/shipments.csv", order.get("order_id"), "missing or UNAVAILABLE")],
            )
        )
        reply = compose(
            ticket,
            {
                "outcome": "UNABLE_TO_VERIFY",
                "reason_codes": ["CARRIER_STATUS_UNAVAILABLE"],
                "order_id": order.get("order_id"),
            },
        )
        return _result(
            analysis,
            resolved,
            "UNABLE_TO_VERIFY",
            ["CARRIER_STATUS_UNAVAILABLE"],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    status = (shipment.get("status") or "").upper()
    delivered_at = shipment.get("delivered_at")
    if status == "DELIVERED" and analysis.non_delivery:
        reply = compose(
            ticket,
            {
                "outcome": "UNABLE_TO_VERIFY",
                "order_id": order.get("order_id"),
                "delivered_at": (delivered_at or "")[:10],
                "delivered_dispute": True,
            },
        )
        return _result(
            analysis,
            resolved,
            "UNABLE_TO_VERIFY",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    if not delivered_at:
        checks.append(
            check(
                "DELIVERY_SCAN",
                "UNABLE_TO_VERIFY",
                "RP-3.2 §2.1/§4.2",
                [evidence("data/shipments.csv", shipment.get("shipment_id"), f"status={status} delivered_at=missing")],
            )
        )
        reply = compose(ticket, {"outcome": "UNABLE_TO_VERIFY", "order_id": order.get("order_id")})
        return _result(
            analysis,
            resolved,
            "UNABLE_TO_VERIFY",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    checks.append(
        check(
            "DELIVERY_SCAN",
            "PASS",
            "RP-3.2 §2.1",
            [evidence("data/shipments.csv", shipment.get("shipment_id"), f"DELIVERED {delivered_at}")],
        )
    )
    delivered_date = parse_date((delivered_at or "")[:10])
    days = days_between(received_date, delivered_date)
    if days is None or days < 0:
        checks.append(
            check(
                "RETURN_WINDOW",
                "UNABLE_TO_VERIFY",
                "RP-3.2 §2.1",
                [evidence(POLICY_DOC, "§2.1", f"received={received_date} delivered={delivered_at}")],
            )
        )
        reply = compose(ticket, {"outcome": "UNABLE_TO_VERIFY", "order_id": order.get("order_id")})
        return _result(
            analysis,
            resolved,
            "UNABLE_TO_VERIFY",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    line = _match_line(order, text, analysis.product_hint)
    if line is None:
        checks.append(
            check(
                "ITEM_IN_ORDER",
                "UNABLE_TO_VERIFY",
                "RP-3.2 §2.2/§2.4",
                [evidence("data/order_items.csv", order.get("order_id"), "no single matching affected line")],
            )
        )
        reply = compose(ticket, {"outcome": "UNABLE_TO_VERIFY", "order_id": order.get("order_id")})
        return _result(
            analysis,
            resolved,
            "UNABLE_TO_VERIFY",
            [],
            checks,
            False,
            None,
            {"action": "REPLY", "amount_usd": None, "line_no": None, "item": None, "status": "APPROVED"},
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref},
        )

    line_no = int(line.get("line_no") or 0)
    item = line.get("product_name") or "the affected item"
    checks.append(
        check(
            "ITEM_IN_ORDER",
            "PASS",
            "RP-3.2 §2.2/§2.4",
            [evidence("data/order_items.csv", f"{order.get('order_id')}#{line_no}", item)],
        )
    )

    damage = analysis.damage
    exception_eligible = damage and has_photo and 31 <= days <= 45 and not baseline_without_damage_exception
    if days <= 30:
        checks.append(
            check(
                "RETURN_WINDOW",
                "PASS",
                "RP-3.2 §2.1",
                [evidence(POLICY_DOC, "§2.1", f"days_since_delivery={days} limit=30")],
            )
        )
        checks.append(check("DAMAGE_PHOTO_EXCEPTION", "NOT_APPLICABLE", "RP-3.2 §2.6", []))
        reason_codes: list[str] = []
    elif exception_eligible:
        checks.append(
            check(
                "RETURN_WINDOW",
                "PASS",
                "RP-3.2 §2.1/§2.6",
                [evidence(POLICY_DOC, "§2.6", f"days_since_delivery={days} damage_photo_exception=31-45")],
            )
        )
        checks.append(
            check(
                "DAMAGE_PHOTO_EXCEPTION",
                "PASS",
                "RP-3.2 §2.6",
                [evidence("ticket", ticket.get("ticket_id", ""), "damage/fault + image attachment")],
            )
        )
        reason_codes = ["DAMAGE_EXCEPTION"]
    else:
        checks.append(
            check(
                "RETURN_WINDOW",
                "FAIL",
                "RP-3.2 §2.1/§2.3/§2.6",
                [
                    evidence(
                        POLICY_DOC,
                        "§2.1/§2.6",
                        f"days_since_delivery={days} limit=30 (45 with damage+photo) damage={damage} photo={has_photo}",
                    )
                ],
            )
        )
        checks.append(
            check(
                "DAMAGE_PHOTO_EXCEPTION",
                "NOT_APPLICABLE" if not damage else "FAIL",
                "RP-3.2 §2.6",
                [evidence("ticket", ticket.get("ticket_id", ""), f"damage={damage} photo={has_photo} days={days}")],
            )
        )
        reply = compose(
            ticket,
            {
                "outcome": "REJECT",
                "reason_codes": ["OUT_OF_WINDOW"],
                "order_id": order.get("order_id"),
                "item": item,
                "days": days,
                "delivered_at": (delivered_at or "")[:10],
                "damage": damage,
                "has_photo": has_photo,
            },
        )
        return _result(
            analysis,
            resolved,
            "REJECT",
            ["OUT_OF_WINDOW"],
            checks,
            False,
            None,
            {
                "action": "REFUND",
                "amount_usd": 0.0,
                "line_no": line_no,
                "item": item,
                "status": "REJECTED",
            },
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref, "days": days, "line_no": line_no},
        )

    # ---- amount, prior refunds, partial lines --------------------------
    ordered_qty = int(line.get("qty") or 1)
    qty = _affected_qty(analysis, line, text)
    checks.append(
        check(
            "AFFECTED_QUANTITY",
            "PASS" if 0 < qty <= ordered_qty else "FAIL",
            "RP-3.2 §2.5",
            [evidence("data/order_items.csv", f"{order.get('order_id')}#{line_no}", f"affected={qty} ordered={ordered_qty}")],
        )
    )

    prior_lines = data.get("refunds", {}).get(order.get("order_id"), {})
    prior_line = int(prior_lines.get(str(line_no), 0))
    prior_local = int(local_refunds.get((order.get("order_id"), line_no), 0))
    prior_total = prior_line + prior_local
    line_total = int(line.get("unit_price_cents") or 0) * ordered_qty
    unpaid = max(0, line_total - prior_total)
    amount = min(int(line.get("unit_price_cents") or 0) * qty, unpaid)

    order_has_prior = any(int(value) > 0 for value in prior_lines.values()) or any(
        key[0] == order.get("order_id") and int(value) > 0 for key, value in local_refunds.items()
    )
    add_already_refunded = prior_line > 0 or (analysis.prior_refund_reference and order_has_prior)

    if prior_line > 0 or prior_local > 0:
        checks.append(
            check(
                "PRIOR_REFUND",
                "PASS" if amount > 0 else "FAIL",
                "RP-3.2 §2.5",
                [
                    evidence(
                        "data/refunds.csv + ledger",
                        f"{order.get('order_id')}#{line_no}",
                        f"issued_or_approved_cents={prior_total} line_total_cents={line_total}",
                    )
                ],
            )
        )
        if add_already_refunded and "ALREADY_REFUNDED" not in reason_codes:
            reason_codes.append("ALREADY_REFUNDED")
    else:
        checks.append(check("PRIOR_REFUND", "NOT_APPLICABLE", "RP-3.2 §2.5", []))
        if add_already_refunded:
            reason_codes.append("ALREADY_REFUNDED")

    checks.append(
        check(
            "REFUND_AMOUNT",
            "PASS" if amount >= 0 else "FAIL",
            "RP-3.2 §2.2/§2.5",
            [
                evidence(
                    "computed",
                    f"{order.get('order_id')}#{line_no}",
                    f"unit_price_cents={line.get('unit_price_cents')} affected_qty={qty} prior_cents={prior_total} amount_cents={amount}",
                )
            ],
        )
    )

    if amount <= 0:
        # Nothing left to pay. Explain prior credits separately when present.
        other_note = ""
        if order_has_prior and not prior_line:
            other_note = _other_prior_note(order, data, line_no)
            if "ALREADY_REFUNDED" not in reason_codes:
                reason_codes.append("ALREADY_REFUNDED")
        reply = compose(
            ticket,
            {
                "outcome": "REJECT",
                "reason_codes": reason_codes + ["ALREADY_REFUNDED"],
                "order_id": order.get("order_id"),
                "item": item,
                "prior_cents": prior_line,
                "other_prior_note": other_note,
            },
        )
        return _result(
            analysis,
            resolved,
            "REJECT",
            _dedupe(reason_codes + ["ALREADY_REFUNDED"]),
            checks,
            False,
            None,
            {
                "action": "REFUND",
                "amount_usd": 0.0,
                "line_no": line_no,
                "item": item,
                "status": "REJECTED",
            },
            reply,
            {"order_id": order.get("order_id"), "requested_ref": requested_ref, "line_no": line_no, "prior_cents": prior_line},
        )

    order_total = int(order.get("total_cents") or 0)
    full_line_total = int(line.get("unit_price_cents") or 0) * ordered_qty
    modified = False
    if analysis.whole_order and len(order.get("lines") or []) > 1 and full_line_total < order_total:
        modified = True
        reason_codes.append("PARTIAL_LINES")
    if analysis.shipping_request:
        modified = True
        reason_codes.append("PARTIAL_LINES")

    if amount > AUTO_LIMIT_CENTS:
        checks.append(
            check(
                "AUTHORITY_LIMIT",
                "FAIL",
                "RP-3.2 §3.2",
                [evidence(POLICY_DOC, "§3.2", f"amount_cents={amount} limit_cents={AUTO_LIMIT_CENTS} approval=SUPPORT_LEAD")],
            )
        )
        reason_codes.append("APPROVAL_REQUIRED")
        reason_codes = _dedupe(reason_codes)
        other_note = _other_prior_note(order, data, line_no) if order_has_prior and not prior_line else ""
        reply = compose(
            ticket,
            {
                "outcome": "PEND",
                "approver_role": "SUPPORT_LEAD",
                "reason_codes": reason_codes,
                "order_id": order.get("order_id"),
                "item": item,
                "amount_cents": amount,
                "prior_cents": prior_line,
                "other_prior_note": other_note,
                "whole_order": analysis.whole_order,
                "shipping_request": analysis.shipping_request,
            },
        )
        return _result(
            analysis,
            resolved,
            "PEND",
            reason_codes,
            checks,
            True,
            "SUPPORT_LEAD",
            {"action": "REFUND", "amount_usd": round(amount / 100, 2), "line_no": line_no, "item": item, "status": "PROPOSED"},
            reply,
            {
                "order_id": order.get("order_id"),
                "requested_ref": requested_ref,
                "line_no": line_no,
                "amount_cents": amount,
                "prior_cents": prior_line,
            },
        )

    checks.append(
        check(
            "AUTHORITY_LIMIT",
            "PASS",
            "RP-3.2 §3.1",
            [evidence(POLICY_DOC, "§3.1", f"amount_cents={amount} limit_cents={AUTO_LIMIT_CENTS}")],
        )
    )
    reason_codes = _dedupe(reason_codes)
    outcome = "PROCEED_MODIFIED" if modified else "PROCEED"
    other_note = _other_prior_note(order, data, line_no) if order_has_prior and not prior_line else ""
    reply = compose(
        ticket,
        {
            "outcome": outcome,
            "order_id": order.get("order_id"),
            "item": item,
            "amount_cents": amount,
            "reason_codes": reason_codes,
            "prior_cents": prior_line,
            "other_prior_note": other_note,
            "whole_order": analysis.whole_order,
            "shipping_request": analysis.shipping_request,
        },
    )
    return _result(
        analysis,
        resolved,
        outcome,
        reason_codes,
        checks,
        False,
        None,
        {"action": "REFUND", "amount_usd": round(amount / 100, 2), "line_no": line_no, "item": item, "status": "APPROVED"},
        reply,
        {
            "order_id": order.get("order_id"),
            "requested_ref": requested_ref,
            "line_no": line_no,
            "amount_cents": amount,
            "prior_cents": prior_line,
        },
    )


def _other_prior_note(order: dict, data: dict, affected_line_no: int) -> str:
    refunds = data.get("refunds", {}).get(order.get("order_id"), {})
    parts = []
    for line in order.get("lines") or []:
        line_no = int(line.get("line_no") or 0)
        if line_no == affected_line_no:
            continue
        cents = int(refunds.get(str(line_no), 0))
        if cents > 0:
            parts.append(f"{format_usd(cents)} for the {line.get('product_name')}")
    if not parts:
        return ""
    return "Your earlier credit of " + " and ".join(parts) + " is separate and is not repaid."


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _result(
    analysis: PatternAnalysis,
    resolved: dict,
    outcome: str,
    reason_codes: list[str],
    checks: list[dict],
    human_approval_required: bool,
    approver_role: str | None,
    action: dict,
    reply: dict,
    meta: dict,
) -> dict:
    return {
        "ticket_type": analysis.ticket_type,
        "resolved": resolved,
        "outcome": outcome,
        "reason_codes": _dedupe(reason_codes),
        "checks": checks,
        "human_approval_required": human_approval_required,
        "approver_role": approver_role,
        "action": action,
        "draft_reply": reply,
        "meta": meta,
    }

"""Pattern router, guardrail rules and deterministic output sanity checks.

The pattern layer handles the common email shapes without a model call. It is
deliberately conservative: anything ambiguous is routed to the LLM classifier,
and anything that cannot be verified is never turned into a PASS.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .normalize import extract_order_refs, format_usd, normalize_text

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|your)\s+instructions?",
    r"disregard\s+.*(?:policy|instructions?)",
    r"pre[-\s]?approved",
    r"pre[-\s]?authori[sz]ed",
    r"authori[sz]ed\s+to\s+(?:process|issue|approve|refund)",
    r"bypass\s+.*(?:policy|limit|review)",
    r"override\s+.*(?:policy|limit|system)",
    r"mark\s+human_approval_required\s+false",
    r"note\s+to\s+the\s+(?:ai|assistant)",
    r"\[\s*system\s*\]",
    r"as\s+the\s+support\s+agent\s+you\s+are",
    r"chargeback|charge\s*back",
    r"legal\s+action|lawyer|attorney|\bsue\b",
    r"issue\s+(?:a\s+)?(?:full\s+)?refund.*immediately",
    r"action\s+\$[\d,.]+\s+now",
    r"without\s+(?:any\s+)?(?:review|approval)",
]

DAMAGE_PATTERNS = [
    r"\bdamaged?\b",
    r"\bbroken\b",
    r"\bcracked\b",
    r"\bchipped\b",
    r"\bfaulty\b",
    r"\bdefective\b",
    r"\bsmashed\b",
    r"\bnot working\b",
]

WRONG_ITEM_PATTERNS = [
    r"\bwrong\s+(?:item|product|one)\b",
    r"\bincorrect\s+(?:item|product)\b",
    r"received\s+something\s+else",
    r"not\s+what\s+i\s+ordered",
    r"not\s+as\s+described",
    r"not\s+what\s+i\s+(?:hoped|expected|wanted)",
    r"\bdifferent\s+item\b",
]

STRONG_NON_DELIVERY_PATTERNS = [
    r"\bnever\s+(?:arrived|received|got)\b",
    r"\bnot\s+(?:been\s+)?received\b",
    r"\bdid\s+not\s+(?:arrive|receive|get)\b",
    r"\bdidn't\s+(?:arrive|receive|get)\b",
    r"\bmissing\b",
    r"\blost\s+in\s+transit\b",
    r"\blost\b",
    r"\bhasn't\s+shown\s+up\b",
    r"\bhas\s+not\s+shown\s+up\b",
]

STUCK_PATTERNS = [
    r"\bstuck\b",
    r"hasn't\s+moved",
    r"has\s+not\s+moved",
    r"no\s+movement",
    r"tracking\s+hasn't",
    r"tracking\s+has\s+not",
    r"no\s+(?:tracking\s+)?update",
    r"hasn't\s+changed",
    r"has\s+not\s+changed",
    r"for\s+ages",
]

STATUS_PATTERNS = [
    r"where(?:'s|\s+is)\s+my\s+order",
    r"\btracking\b",
    r"\bstatus\b",
    r"when\s+will",
    r"has\s+my\s+order",
    r"delivery\s+update",
]

RETURN_PATTERNS = [
    r"\breturn(?:ing)?\b",
    r"send\s+(?:it\s+)?back",
    r"return\s+label",
]

REFUND_PATTERNS = [
    r"\brefund(?:s|ed|ing)?\b",
    r"money\s+back",
    r"\breimburse",
    r"\bcredit\b",
    r"\bcompensat",
]

WHOLE_ORDER_PATTERNS = [
    r"whole\s+order",
    r"entire\s+order",
    r"full\s+order",
    r"money\s+back\s+for\s+the\s+(?:whole|entire)\s+order",
]

SHIPPING_PATTERNS = [
    r"shipping\s+(?:charge|fee|cost)",
    r"delivery\s+(?:charge|fee|cost)",
    r"\bpostage\b",
]

PRODUCT_TARGET_RE = re.compile(
    r"(?:refund|return|replace|credit)\s+(?:just\s+)?(?:the\s+)?"
    r"(?:broken|damaged|faulty|defective|cracked)?\s*"
    r"([a-z][a-z0-9()\- ]{1,50}?)(?=[.,;!\n]|$)",
    re.IGNORECASE,
)

PRIOR_REFUND_REFERENCE_RE = re.compile(
    r"(?:your|the)\s+(?:\$[\d,.]+\s+)?[\w\s-]{0,20}refund|"
    r"refund\s+(?:has|have)\s+(?:come|been)|"
    r"already\s+refund|"
    r"refunded\b|"
    r"\bagain\b",
    re.IGNORECASE,
)

ONE_OF_RE = re.compile(r"\bone\s+of\s+(?:my\s+)?(?:two|2|\d+)", re.IGNORECASE)
BOTH_RE = re.compile(r"\bboth\b", re.IGNORECASE)


@dataclass
class PatternAnalysis:
    ticket_type: str
    order_refs: list[str] = field(default_factory=list)
    product_hint: str | None = None
    damage: bool = False
    wrong_item: bool = False
    non_delivery: bool = False
    stuck: bool = False
    status_request: bool = False
    return_request: bool = False
    refund_request: bool = False
    whole_order: bool = False
    shipping_request: bool = False
    injection: bool = False
    injection_matches: list[str] = field(default_factory=list)
    prior_refund_reference: bool = False
    affected_qty: int | None = None  # None = default 1, -1 = all ordered units
    confidence: float = 0.0
    route: str = "pattern"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _matches(text: str, patterns: list[str]) -> list[str]:
    found = []
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(pattern)
    return found


def _product_hint(text: str) -> str | None:
    matches = [match.group(1).strip() for match in PRODUCT_TARGET_RE.finditer(text)]
    if not matches:
        return None
    # Prefer the longest concrete phrase, skipping pronouns.
    matches = [match for match in matches if match.lower() not in {"it", "that", "this", "them"}]
    if not matches:
        return None
    return max(matches, key=len)


def analyse(ticket: dict) -> PatternAnalysis:
    text = normalize_text(f"{ticket.get('subject', '')} {ticket.get('body', '')}")
    order_refs = extract_order_refs(text)

    injection_matches = _matches(text, INJECTION_PATTERNS)
    damage = bool(_matches(text, DAMAGE_PATTERNS))
    wrong_item = bool(_matches(text, WRONG_ITEM_PATTERNS))
    non_delivery = bool(_matches(text, STRONG_NON_DELIVERY_PATTERNS))
    stuck = bool(_matches(text, STUCK_PATTERNS))
    status_request = bool(_matches(text, STATUS_PATTERNS))
    return_request = bool(_matches(text, RETURN_PATTERNS))
    refund_request = bool(_matches(text, REFUND_PATTERNS))
    whole_order = bool(_matches(text, WHOLE_ORDER_PATTERNS))
    shipping_request = bool(_matches(text, SHIPPING_PATTERNS))
    prior_reference = bool(PRIOR_REFUND_REFERENCE_RE.search(text))

    delivery_issue = stuck or non_delivery or "delivery issue" in text

    if refund_request or damage or wrong_item:
        ticket_type = "REFUND"
    elif return_request:
        ticket_type = "RETURN"
    elif delivery_issue:
        ticket_type = "DELIVERY_ISSUE"
    elif status_request:
        ticket_type = "ORDER_STATUS"
    else:
        ticket_type = "OTHER"

    affected_qty: int | None = None
    if ONE_OF_RE.search(text):
        affected_qty = 1
    elif BOTH_RE.search(text):
        affected_qty = -1

    confidence = 0.92
    if not order_refs:
        confidence -= 0.40
    if len(order_refs) > 1:
        confidence -= 0.45
    if ticket_type == "OTHER":
        confidence -= 0.45
    if not any([damage, wrong_item, refund_request, return_request, status_request, delivery_issue]):
        confidence -= 0.20
    if injection_matches:
        confidence = 0.98
    confidence = max(0.05, min(0.99, confidence))

    if injection_matches:
        route = "guardrail"
    elif confidence >= 0.60:
        route = "pattern"
    else:
        route = "llm"

    return PatternAnalysis(
        ticket_type=ticket_type,
        order_refs=order_refs,
        product_hint=_product_hint(text),
        damage=damage,
        wrong_item=wrong_item,
        non_delivery=non_delivery,
        stuck=stuck,
        status_request=status_request,
        return_request=return_request,
        refund_request=refund_request,
        whole_order=whole_order,
        shipping_request=shipping_request,
        injection=bool(injection_matches),
        injection_matches=injection_matches,
        prior_refund_reference=prior_reference,
        affected_qty=affected_qty,
        confidence=round(confidence, 2),
        route=route,
    )


def sanity_check(response: dict) -> list[str]:
    """Deterministic final sanity gate. Any issue means fail closed."""
    issues: list[str] = []
    outcome = response.get("outcome")
    checks = response.get("checks", [])
    action = response.get("action") or {}
    amount_cents = int(round(float(action.get("amount_usd") or 0) * 100))
    human = bool(response.get("human_approval_required"))
    role = response.get("approver_role")
    body = ((response.get("draft_reply") or {}).get("body") or "").lower()
    resolved = response.get("resolved") or {}

    if outcome in {"PEND", "ESCALATE"} and not human:
        issues.append("approval_required_flag_missing")
    if outcome not in {"PEND", "ESCALATE"} and human:
        issues.append("approval_flag_set_without_queue")
    if outcome == "PEND" and role != "SUPPORT_LEAD":
        issues.append("pend_role_not_support_lead")
    if outcome == "ESCALATE" and role not in {"CARRIER_CLAIMS", "TRUST_SAFETY"}:
        issues.append("escalate_role_missing")
    if amount_cents > 15000 and outcome not in {"PEND", "ESCALATE", "REJECT"}:
        issues.append("above_limit_without_approval")
    if outcome == "INVALID_REQUEST" and amount_cents > 0:
        issues.append("invalid_request_with_amount")
    if amount_cents > 0 and action.get("action") != "REFUND":
        issues.append("amount_without_refund_action")
    if amount_cents > 0 and format_usd(amount_cents).lower() not in body:
        issues.append("reply_amount_mismatch")
    if amount_cents > 0 and resolved.get("order_id") and resolved["order_id"].lower() not in body:
        issues.append("reply_order_mismatch")
    if any(check.get("result") == "UNABLE_TO_VERIFY" for check in checks) and outcome == "PROCEED" and amount_cents > 0:
        issues.append("proceed_with_unverified_fact")
    if outcome == "PROCEED" and any(check.get("result") == "FAIL" for check in checks) and amount_cents > 0:
        issues.append("proceed_with_failed_check")
    if any(check.get("result") == "PASS" and check.get("incomplete") for check in checks):
        issues.append("pass_on_incomplete_check")
    return issues

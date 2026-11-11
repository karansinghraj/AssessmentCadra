"""Normalisation helpers shared by data preparation, routing and policy.

The export is deliberately dirty. Every rule here is documented in
ARCHITECTURE.md §4.5 and is applied identically for all tickets.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

ORDER_RE = re.compile(r"(?<![\w.])(?:LF[-\s]?)?(\d{6})(?!\d)", re.IGNORECASE)
EMAIL_CLEAN_RE = re.compile(r"\s+")
MONEY_CLEAN_RE = re.compile(r"[^0-9.\-]")


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_order_id(value: str | None) -> str | None:
    """Return canonical LF-###### from any observed order reference."""
    if not value:
        return None
    match = re.search(r"(\d{6})", str(value))
    if not match:
        return None
    return f"LF-{match.group(1)}"


def extract_order_refs(text: str | None) -> list[str]:
    """Extract distinct canonical order ids in first-seen order.

    Handles LF-100200, LF100201, #100202, order 100203, order order 100204.
    The 6-digit guard avoids money, quantities and dates.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in ORDER_RE.finditer(text):
        canonical = f"LF-{match.group(1)}"
        if canonical not in seen:
            seen.add(canonical)
            found.append(canonical)
    return found


def parse_money_cents(value: str | None) -> int | None:
    """Parse '$129.00', 'USD 24.00', '258.00' or '' into integer cents."""
    if value is None:
        return None
    cleaned = MONEY_CLEAN_RE.sub("", str(value))
    if cleaned in {"", "-", "."}:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return int((amount * 100).to_integral_value())


def format_usd(cents: int | None) -> str:
    if cents is None:
        return "$0.00"
    return f"${cents / 100:,.2f}"


def detect_slash_format(values: list[str]) -> str:
    """Infer DD/MM vs MM/DD for a whole column.

    The export contains 26/08, 30/09 and 27/10, which prove DD/MM. If a column
    were provably MM/DD we would use it; otherwise the documented default is
    DD/MM for this export.
    """
    first_gt_12 = False
    second_gt_12 = False
    for raw in values:
        raw = (raw or "").strip()
        if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            continue
        first, second, _year = raw.split("/")
        if int(first) > 12:
            first_gt_12 = True
        if int(second) > 12:
            second_gt_12 = True
    if first_gt_12 and not second_gt_12:
        return "DD/MM"
    if second_gt_12 and not first_gt_12:
        return "MM/DD"
    return "DD/MM"


def parse_date(value: str | None, slash_format: str = "DD/MM") -> date | None:
    """Parse ISO dates/timestamps and the export's slash dates."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed.date()
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if not match:
        return None
    first, second, year = (int(part) for part in match.groups())
    if slash_format == "MM/DD":
        month, day = first, second
    else:
        day, month = first, second
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def to_date(value: str | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_date(str(value))


def days_between(later: date | datetime | str | None, earlier: date | datetime | str | None) -> int | None:
    later_date = to_date(later)
    earlier_date = to_date(earlier)
    if later_date is None or earlier_date is None:
        return None
    return (later_date - earlier_date).days


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def contains_any(text: str, patterns: list[str]) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text, re.IGNORECASE)]

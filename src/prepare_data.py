"""Reproducible data preparation for the Larkfield data room.

Run:  python -m src.prepare_data

Reads data/*.csv without modifying them and writes build/data.json plus
build/manifest.json. The output is deterministic (sorted keys, no timestamps).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .normalize import (
    detect_slash_format,
    normalize_email,
    normalize_order_id,
    parse_date,
    parse_datetime,
    parse_money_cents,
)

SOURCE_FILES = ["customers.csv", "orders.csv", "order_items.csv", "shipments.csv", "refunds.csv"]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_iso(value: str | None, slash_format: str) -> str | None:
    parsed = parse_date(value, slash_format)
    return parsed.isoformat() if parsed else None


def build_data(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    customers_raw = _read_csv(data_dir / "customers.csv")
    orders_raw = _read_csv(data_dir / "orders.csv")
    items_raw = _read_csv(data_dir / "order_items.csv")
    shipments_raw = _read_csv(data_dir / "shipments.csv")
    refunds_raw = _read_csv(data_dir / "refunds.csv")

    issues: dict[str, object] = {}

    # ---- customers -----------------------------------------------------
    customers: dict[str, dict] = {}
    customers_by_email: dict[str, str] = {}
    duplicate_emails: list[str] = []
    for row in customers_raw:
        customer_id = (row.get("customer_id") or "").strip()
        if not customer_id:
            continue
        email = normalize_email(row.get("email"))
        customers[customer_id] = {
            "customer_id": customer_id,
            "name": (row.get("name") or "").strip(),
            "email": email,
            "country": (row.get("country") or "").strip(),
            "created_at": (row.get("created_at") or "").strip(),
            "loyalty_tier": (row.get("loyalty_tier") or "").strip(),
        }
        if email in customers_by_email and customers_by_email[email] != customer_id:
            duplicate_emails.append(email)
        else:
            customers_by_email[email] = customer_id
    issues["duplicate_customer_emails"] = sorted(set(duplicate_emails))

    # ---- orders --------------------------------------------------------
    order_date_values = [(row.get("order_date") or "").strip() for row in orders_raw]
    slash_format = detect_slash_format(order_date_values)
    money_formats = Counter()
    for row in orders_raw:
        raw_total = (row.get("Order Total") or "").strip()
        if "$" in raw_total:
            money_formats["dollar_symbol"] += 1
        elif "USD" in raw_total.upper():
            money_formats["usd_prefix"] += 1
        elif raw_total:
            money_formats["plain"] += 1

    orders_grouped: dict[str, list[dict]] = defaultdict(list)
    unknown_order_rows: list[dict] = []
    for row in orders_raw:
        order_id = normalize_order_id(row.get("order_id"))
        if not order_id:
            unknown_order_rows.append(row)
            continue
        orders_grouped[order_id].append(row)

    orders: dict[str, dict] = {}
    duplicate_orders: list[str] = []
    conflicting_duplicates: list[str] = []
    for order_id, rows in orders_grouped.items():
        normalised_rows = []
        for row in rows:
            normalised_rows.append(
                {
                    "order_id": order_id,
                    "customer_id": (row.get("customer_id") or "").strip(),
                    "order_date": _date_iso(row.get("order_date"), slash_format),
                    "status": (row.get("status") or "").strip().upper(),
                    "total_cents": parse_money_cents(row.get("Order Total")),
                    "shipping_fee_cents": parse_money_cents(row.get("shipping_fee")),
                    "currency": (row.get("currency") or "").strip().upper(),
                    "payment_method": (row.get("payment_method") or "").strip(),
                }
            )
        if len(rows) > 1:
            duplicate_orders.append(order_id)
        first = normalised_rows[0]
        if any(candidate != first for candidate in normalised_rows[1:]):
            conflicting_duplicates.append(order_id)
            first = dict(first, quarantined=True)
        else:
            first = dict(first, quarantined=False)
        first["duplicate_rows"] = len(rows)
        orders[order_id] = first
    issues["duplicate_order_ids_collapsed"] = sorted(duplicate_orders)
    issues["conflicting_duplicate_orders"] = sorted(conflicting_duplicates)
    issues["unknown_order_rows"] = len(unknown_order_rows)

    # ---- order lines ---------------------------------------------------
    items_grouped: dict[str, list[dict]] = defaultdict(list)
    for row in items_raw:
        order_id = normalize_order_id(row.get("order_id"))
        if not order_id:
            continue
        line_no = int((row.get("line_no") or "0").strip() or 0)
        qty = int(float((row.get("qty") or "0").strip() or 0))
        unit_cents = parse_money_cents(row.get("unit_price")) or 0
        items_grouped[order_id].append(
            {
                "line_no": line_no,
                "sku": (row.get("sku") or "").strip(),
                "product_name": (row.get("product_name") or "").strip(),
                "qty": qty,
                "unit_price_cents": unit_cents,
                "line_total_cents": qty * unit_cents,
            }
        )

    total_mismatches: list[dict] = []
    for order_id, order in orders.items():
        lines = sorted(items_grouped.get(order_id, []), key=lambda line: line["line_no"])
        order["lines"] = lines
        line_total = sum(line["line_total_cents"] for line in lines)
        if order.get("total_cents") is not None and line_total != order["total_cents"]:
            total_mismatches.append(
                {"order_id": order_id, "order_total_cents": order["total_cents"], "line_total_cents": line_total}
            )
    issues["order_total_vs_lines_mismatches"] = total_mismatches

    # ---- shipments -----------------------------------------------------
    shipments_grouped: dict[str, list[dict]] = defaultdict(list)
    for row in shipments_raw:
        order_id = normalize_order_id(row.get("order_id"))
        if not order_id:
            continue
        shipments_grouped[order_id].append(
            {
                "shipment_id": (row.get("shipment_id") or "").strip(),
                "order_id": order_id,
                "carrier": (row.get("carrier") or "").strip(),
                "tracking_no": (row.get("tracking_no") or "").strip(),
                "status": (row.get("status") or "").strip().upper(),
                "shipped_at": (row.get("shipped_at") or "").strip() or None,
                "delivered_at": (row.get("delivered_at") or "").strip() or None,
                "last_event_at": (row.get("last_event_at") or "").strip() or None,
            }
        )
    shipments: dict[str, dict] = {}
    multi_shipments: list[str] = []
    scan_before_ship: list[str] = []
    for order_id, rows in shipments_grouped.items():
        if len(rows) > 1:
            multi_shipments.append(order_id)
        shipment = rows[0]
        shipped = parse_datetime(shipment.get("shipped_at"))
        last_event = parse_datetime(shipment.get("last_event_at"))
        shipment["scan_before_ship"] = bool(shipped and last_event and last_event < shipped)
        if shipment["scan_before_ship"]:
            scan_before_ship.append(order_id)
        shipments[order_id] = shipment
    issues["multi_shipment_orders"] = sorted(multi_shipments)
    issues["scans_before_shipped_at"] = sorted(scan_before_ship)
    issues["missing_shipments"] = sorted(set(orders) - set(shipments))
    issues["unavailable_shipments"] = sorted(
        order_id for order_id, shipment in shipments.items() if shipment["status"] == "UNAVAILABLE"
    )

    # ---- refunds -------------------------------------------------------
    refunds: dict[str, dict[str, int]] = defaultdict(dict)
    refund_rows: list[dict] = []
    for row in refunds_raw:
        order_id = normalize_order_id(row.get("order_id"))
        if not order_id:
            continue
        line_no = str(int((row.get("line_no") or "0").strip() or 0))
        cents = parse_money_cents(row.get("amount")) or 0
        status = (row.get("status") or "").strip().upper()
        refund_rows.append(
            {
                "refund_id": (row.get("refund_id") or "").strip(),
                "order_id": order_id,
                "line_no": line_no,
                "amount_cents": cents,
                "status": status,
                "requested_at": (row.get("requested_at") or "").strip(),
                "approved_by": (row.get("approved_by") or "").strip(),
            }
        )
        if status == "ISSUED":
            refunds[order_id][line_no] = refunds[order_id].get(line_no, 0) + cents
    issues["issued_refunds"] = refund_rows

    # ---- manifest ------------------------------------------------------
    manifest = {
        "source": "Larkfield data room export 2026-11-02",
        "source_hashes": {name: _sha256(data_dir / name) for name in SOURCE_FILES},
        "counts": {
            "customers": len(customers),
            "order_rows": len(orders_raw),
            "unique_orders": len(orders),
            "order_lines": len(items_raw),
            "shipments": len(shipments),
            "refund_rows": len(refund_rows),
        },
        "rules": {
            "order_id": "canonical LF-######; strip LF-/LF/#/text, zero-pad 6 digits",
            "dates": f"ISO first; slash dates parsed as {slash_format} (inferred from unambiguous values)",
            "money": "strip $, USD and commas; store integer cents",
            "duplicate_orders": "collapse identical rows; quarantine conflicting rows",
            "refunds": "sum only ISSUED refunds per (order_id, line_no)",
        },
        "formats": {
            "slash_date_format": slash_format,
            "slash_date_values": sum(1 for value in order_date_values if "/" in value),
            "money": dict(sorted(money_formats.items())),
        },
        "issues": issues,
    }
    return {
        "manifest": manifest,
        "customers": customers,
        "customers_by_email": customers_by_email,
        "orders": orders,
        "shipments": shipments,
        "refunds": {order_id: dict(lines) for order_id, lines in refunds.items()},
    }


def write_data(data: dict, build_dir: Path) -> None:
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    manifest = data["manifest"]
    (build_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (build_dir / "data.json").write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_manifest(build_dir: Path) -> dict | None:
    path = Path(build_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def source_hashes(data_dir: Path) -> dict[str, str]:
    data_dir = Path(data_dir)
    return {name: _sha256(data_dir / name) for name in SOURCE_FILES}


def ensure_built(data_dir: Path, build_dir: Path) -> dict:
    """Build if missing or if any source file changed. Never edits sources."""
    manifest = load_manifest(build_dir)
    current = source_hashes(data_dir)
    if manifest and manifest.get("source_hashes") == current:
        data_path = Path(build_dir) / "data.json"
        if data_path.exists():
            return json.loads(data_path.read_text(encoding="utf-8"))
    data = build_data(data_dir)
    write_data(data, build_dir)
    return data


def report(data: dict) -> str:
    manifest = data["manifest"]
    counts = manifest["counts"]
    issues = manifest["issues"]
    lines = [
        "Larkfield data prepared (sources untouched)",
        f"  customers={counts['customers']} order_rows={counts['order_rows']} "
        f"unique_orders={counts['unique_orders']} lines={counts['order_lines']} "
        f"shipments={counts['shipments']} refunds={counts['refund_rows']}",
        f"  slash dates={manifest['formats']['slash_date_values']} format={manifest['formats']['slash_date_format']} "
        f"money={manifest['formats']['money']}",
        f"  duplicate orders collapsed={issues['duplicate_order_ids_collapsed']}",
        f"  conflicting duplicates={issues['conflicting_duplicate_orders']}",
        f"  missing shipments={issues['missing_shipments']}",
        f"  scans before shipped_at={issues['scans_before_shipped_at']}",
        f"  unavailable shipments={issues['unavailable_shipments']}",
        f"  issued refunds={len(issues['issued_refunds'])}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the Larkfield data room reproducibly.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--report", action="store_true", help="print the preparation report")
    args = parser.parse_args(argv)

    data = build_data(args.data_dir)
    write_data(data, args.build_dir)
    if args.report:
        print(report(data))
    else:
        print(report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())

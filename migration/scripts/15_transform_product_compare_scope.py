#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from _common import ensure_dir, iter_csv, load_state, log, parse_date, parse_json, save_state, to_float, to_int, write_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform extracted legacy product comparison facts into loader-ready CSV."
    )
    parser.add_argument("--in-dir", required=True, help="Input directory from step 14.")
    parser.add_argument("--out-dir", required=True, help="Output directory for transformed CSV.")
    parser.add_argument("--source-db", default="live_11nov_2024")
    parser.add_argument("--import-batch-id", default="")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def normalize_month(value: str | None) -> str:
    parsed = parse_date(value)
    if not parsed:
        return ""
    return parsed.replace(day=1).isoformat()


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in {"", "false", "none", "null"} else text


def main() -> None:
    args = parse_args()
    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    state_file = Path(args.state_file)

    ensure_dir(out_dir)
    ensure_dir(state_file.parent)

    src_path = in_dir / "legacy_product_month_fact_raw.csv"
    if not src_path.exists():
        raise SystemExit(f"Missing input file: {src_path}")

    out_path = out_dir / "legacy_product_month_fact.csv"
    manifest_path = out_dir / "transform_product_compare_manifest.json"

    state = load_state(state_file) if args.resume else {}
    if args.resume and state.get("status") == "completed" and out_path.exists():
        log("Transform already completed; nothing to do.")
        return

    state.setdefault("steps", {})
    state["status"] = "running"
    save_state(state_file, state)

    batch_id = args.import_batch_id or f"product_compare_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    keyed: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for row in iter_csv(src_path):
        source_db = (row.get("source_db") or args.source_db or "").strip()
        source_product_id = to_int(row.get("source_product_id"), default=0)
        period_month = normalize_month(row.get("period_month"))
        warehouse_key = (row.get("warehouse_key") or "all").strip() or "all"
        if not source_db or source_product_id <= 0 or not period_month:
            continue

        payload = parse_json(row.get("legacy_payload")) or {}
        keyed[(source_db, source_product_id, period_month, warehouse_key)] = {
            "source_db": source_db,
            "source_product_id": source_product_id,
            "period_month": period_month,
            "warehouse_key": warehouse_key,
            "source_default_code": clean_optional_text(row.get("source_default_code")),
            "source_barcode": clean_optional_text(row.get("source_barcode")),
            "source_name": (row.get("source_name") or "").strip() or None,
            "source_category_name": (row.get("source_category_name") or "").strip() or None,
            "source_brand_name": (row.get("source_brand_name") or "").strip() or None,
            "legacy_sales_qty": to_float(row.get("legacy_sales_qty"), default=0.0),
            "legacy_sales_amount": to_float(row.get("legacy_sales_amount"), default=0.0),
            "legacy_untaxed_total_sales_amount": to_float(row.get("legacy_untaxed_total_sales_amount"), default=0.0),
            "legacy_total_sales_amount": to_float(row.get("legacy_total_sales_amount"), default=0.0),
            "legacy_net_total_sales_amount": to_float(row.get("legacy_net_total_sales_amount"), default=0.0),
            "legacy_return_qty": to_float(row.get("legacy_return_qty"), default=0.0),
            "legacy_return_amount": to_float(row.get("legacy_return_amount"), default=0.0),
            "legacy_return_total_amount": to_float(row.get("legacy_return_total_amount"), default=0.0),
            "legacy_discount_reason_sales_untaxed": to_float(row.get("legacy_discount_reason_sales_untaxed"), default=0.0),
            "legacy_discount_reason_sales_total": to_float(row.get("legacy_discount_reason_sales_total"), default=0.0),
            "legacy_discount_amount": to_float(row.get("legacy_discount_amount"), default=0.0),
            "legacy_gross_sales_amount": to_float(row.get("legacy_gross_sales_amount"), default=0.0),
            "legacy_net_sales_amount": to_float(row.get("legacy_net_sales_amount"), default=0.0),
            "legacy_asp": (
                to_float(row.get("legacy_asp"), default=0.0)
                if row.get("legacy_asp") not in (None, "")
                else None
            ),
            "legacy_cogs_amount": (
                to_float(row.get("legacy_cogs_amount"), default=0.0)
                if row.get("legacy_cogs_amount") not in (None, "")
                else None
            ),
            "legacy_margin_amount": (
                to_float(row.get("legacy_margin_amount"), default=0.0)
                if row.get("legacy_margin_amount") not in (None, "")
                else None
            ),
            "legacy_margin_pct": (
                to_float(row.get("legacy_margin_pct"), default=0.0)
                if row.get("legacy_margin_pct") not in (None, "")
                else None
            ),
            "legacy_last_cost_unit": (
                (lambda v: v if (v is not None and v > 0) else None)(
                    to_float(row.get("legacy_last_cost_unit"), default=0.0)
                    if row.get("legacy_last_cost_unit") not in (None, "")
                    else None
                )
            ),
            "legacy_avg_cost_unit": (
                (lambda v: v if (v is not None and v > 0) else None)(
                    to_float(row.get("legacy_avg_cost_unit"), default=0.0)
                    if row.get("legacy_avg_cost_unit") not in (None, "")
                    else None
                )
            ),
            "legacy_cost_available": to_bool(row.get("legacy_cost_available")),
            "legacy_cost_source": (row.get("legacy_cost_source") or "").strip() or None,
            "legacy_margin_comparable": to_bool(row.get("legacy_margin_comparable")),
            "legacy_stock_close_qty": (
                to_float(row.get("legacy_stock_close_qty"), default=0.0)
                if row.get("legacy_stock_close_qty") not in (None, "")
                else None
            ),
            "legacy_stock_close_value": (
                to_float(row.get("legacy_stock_close_value"), default=0.0)
                if row.get("legacy_stock_close_value") not in (None, "")
                else None
            ),
            "value_available": to_bool(row.get("value_available")),
            "import_batch_id": batch_id,
            "legacy_payload": to_json(payload),
        }

    rows_out = [keyed[key] for key in sorted(keyed.keys())]
    write_csv(
        out_path,
        rows_out,
        [
            "source_db",
            "source_product_id",
            "period_month",
            "warehouse_key",
            "source_default_code",
            "source_barcode",
            "source_name",
            "source_category_name",
            "source_brand_name",
            "legacy_sales_qty",
            "legacy_sales_amount",
            "legacy_untaxed_total_sales_amount",
            "legacy_total_sales_amount",
            "legacy_net_total_sales_amount",
            "legacy_return_qty",
            "legacy_return_amount",
            "legacy_return_total_amount",
            "legacy_discount_reason_sales_untaxed",
            "legacy_discount_reason_sales_total",
            "legacy_discount_amount",
            "legacy_gross_sales_amount",
            "legacy_net_sales_amount",
            "legacy_asp",
            "legacy_cogs_amount",
            "legacy_margin_amount",
            "legacy_margin_pct",
            "legacy_last_cost_unit",
            "legacy_avg_cost_unit",
            "legacy_cost_available",
            "legacy_cost_source",
            "legacy_margin_comparable",
            "legacy_stock_close_qty",
            "legacy_stock_close_value",
            "value_available",
            "import_batch_id",
            "legacy_payload",
        ],
        mode="w",
    )

    manifest = {
        "source_db": args.source_db,
        "import_batch_id": batch_id,
        "input_file": str(src_path),
        "output_file": str(out_path),
        "rows": len(rows_out),
        "status": "completed",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    state["steps"]["transform"] = {
        "rows": len(rows_out),
        "input_file": str(src_path),
        "output_file": str(out_path),
        "import_batch_id": batch_id,
    }
    state["status"] = "completed"
    save_state(state_file, state)
    log("Transform completed")


if __name__ == "__main__":
    main()

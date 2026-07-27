#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
import json
from pathlib import Path
from typing import Any

from _common import (
    connect_from_env,
    ensure_dir,
    fail,
    fetch_all,
    get_table_columns,
    load_state,
    log,
    parse_date,
    save_state,
    table_exists,
    to_float,
    to_int,
    write_csv,
)


# VAT divisor used by the Odoo 18 current-side margin (the inventory unit cost is
# stored VAT-inclusive in this company's data). Replicated here so the legacy
# Net Margin is computed the same way on Odoo 12 data: untaxed_cost = cost / 1.14.
UNTAX_COST_DIVISOR = 1.14


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract legacy product comparison monthly facts from Odoo12 (read-only)."
    )
    parser.add_argument("--source-env", default="ODOO12_DSN", help="Env var containing source PostgreSQL DSN.")
    parser.add_argument("--source-db", required=True, help="Source database name override.")
    parser.add_argument("--from-date", default="2025-01-01", help="Inclusive start date (YYYY-MM-DD).")
    parser.add_argument("--to-date", default="2025-12-31", help="Inclusive end date (YYYY-MM-DD).")
    parser.add_argument("--out-dir", required=True, help="Output directory for extracted CSV files.")
    parser.add_argument("--state-file", required=True, help="Checkpoint JSON file.")
    parser.add_argument("--statement-timeout-ms", type=int, default=0, help="Optional DB statement timeout in ms.")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def month_floor(date_value: dt.date) -> dt.date:
    return date_value.replace(day=1)


def next_month(date_value: dt.date) -> dt.date:
    if date_value.month == 12:
        return date_value.replace(year=date_value.year + 1, month=1, day=1)
    return date_value.replace(month=date_value.month + 1, day=1)


def month_sequence(start: dt.date, end: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    cur = month_floor(start)
    final = month_floor(end)
    while cur <= final:
        out.append(cur)
        cur = next_month(cur)
    return out


def detect_brand_join(conn, pt_cols: set[str]) -> tuple[str | None, str | None, bool]:
    candidates = [
        ("product_brand_id", "product_brand"),
        ("brand_id", "product_brand"),
        ("brand_id", "product_branding"),
        ("x_brand_id", "product_brand"),
    ]
    for col_name, table_name in candidates:
        if col_name not in pt_cols:
            continue
        if not table_exists(conn, table_name):
            continue
        brand_cols = set(get_table_columns(conn, table_name))
        if "name" not in brand_cols:
            continue
        return col_name, table_name, True
    return None, None, False


def detect_legacy_cost_source(ail_cols: set[str], pp_cols: set[str]) -> tuple[str | None, str | None]:
    # Prefer invoice-line cost columns when available; fallback to product snapshot cost.
    if "total_cost" in ail_cols:
        return "ail.total_cost", "account_invoice_line.total_cost"
    if "purchase_price" in ail_cols:
        return "ail.purchase_price", "account_invoice_line.purchase_price"
    if "standard_price" in ail_cols:
        return "ail.standard_price", "account_invoice_line.standard_price"
    if "standard_price" in pp_cols:
        return "pp.standard_price", "product_product.standard_price"
    return None, None


def detect_legacy_report_total_source(conn) -> tuple[str | None, str | None]:
    """Company-currency line total from the Odoo12 invoice-analysis view.

    Raw ``account_invoice_line.price_total`` is in the INVOICE's currency, so it
    undercounts foreign-currency (e.g. USD) lines -- e.g. a USD iPhone line stored
    as 6,453.26 USD instead of ~308,768 EGP. The Odoo12 ``account_invoice_report``
    view is one row per invoice line (its ``id`` == ``account_invoice_line.id``) and
    carries ``currency_rate`` (company->document rate, e.g. 0.0209 for USD), so
    ``price_total / currency_rate`` converts the line total to company currency.

    Verified against live Odoo12 to reproduce the migrated invoices' "Total" to the
    cent for MG6J4 Nov/Dec 2025 (31,632,745.77 / 22,912,129.95). Note: do NOT use
    ``air.amount_total`` (over-counts) and do NOT join on ``air.account_line_id``
    (a constant in this customized view, never the line id).
    """
    if not table_exists(conn, "account_invoice_report"):
        return None, None
    cols = set(get_table_columns(conn, "account_invoice_report"))
    if {"id", "currency_rate"}.issubset(cols):
        return (
            "COALESCE(ail.price_total / NULLIF(air.currency_rate, 0), ail.price_total)",
            "account_invoice_report.price_total/currency_rate",
        )
    return None, None


def fetch_stock_move_valuation_units(
    conn,
    from_date: str,
    to_date: str,
) -> dict[tuple[int, str], dict[str, float | None]]:
    if not table_exists(conn, "stock_move") or not table_exists(conn, "stock_location"):
        return {}

    sm_cols = set(get_table_columns(conn, "stock_move"))
    sl_cols = set(get_table_columns(conn, "stock_location"))
    required = {"product_id", "date", "state", "location_id", "location_dest_id", "price_unit"}
    if not required.issubset(sm_cols) or "usage" not in sl_cols:
        return {}

    qty_col = "quantity_done" if "quantity_done" in sm_cols else ("product_uom_qty" if "product_uom_qty" in sm_cols else ("product_qty" if "product_qty" in sm_cols else None))
    if not qty_col:
        return {}

    start_date = parse_date(from_date)
    end_date = parse_date(to_date)
    if not start_date or not end_date:
        return {}

    rows = fetch_all(
        conn,
        f"""
        SELECT
            sm.product_id AS source_product_id,
            sm.date::date AS move_date,
            date_trunc('month', sm.date)::date AS period_month,
            ABS(COALESCE(sm.{qty_col}, 0.0)) AS move_qty,
            COALESCE(sm.price_unit, 0.0) AS unit_cost
        FROM stock_move sm
        LEFT JOIN stock_location src ON src.id = sm.location_id
        LEFT JOIN stock_location dst ON dst.id = sm.location_dest_id
        WHERE sm.product_id IS NOT NULL
          AND sm.state = 'done'
          AND sm.date::date < (%s::date + INTERVAL '1 month')
          AND src.usage = 'supplier'
          AND dst.usage = 'internal'
          AND ABS(COALESCE(sm.{qty_col}, 0.0)) > 0
          AND COALESCE(sm.price_unit, 0.0) > 0
        ORDER BY sm.product_id, sm.date, sm.id
        """,
        (to_date,),
    )

    month_stats: dict[tuple[int, str], dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "value": 0.0})
    moves_by_product: dict[int, list[tuple[dt.date, float]]] = defaultdict(list)
    for row in rows:
        pid = to_int(row.get("source_product_id"), default=0)
        month = str(row.get("period_month") or "")[:10]
        if pid <= 0 or not month:
            continue
        move_date = row.get("move_date")
        if isinstance(move_date, dt.datetime):
            move_date = move_date.date()
        elif not isinstance(move_date, dt.date):
            move_date = parse_date(str(move_date or ""))
        if not move_date:
            continue
        qty = abs(to_float(row.get("move_qty"), default=0.0))
        unit_cost = to_float(row.get("unit_cost"), default=0.0)
        if qty <= 0 or unit_cost <= 0:
            continue
        stats = month_stats[(pid, month)]
        stats["qty"] += qty
        stats["value"] += qty * unit_cost
        moves_by_product[pid].append((move_date, unit_cost))

    months = month_sequence(month_floor(start_date), month_floor(end_date))
    out: dict[tuple[int, str], dict[str, float | None]] = {}
    for pid, moves in moves_by_product.items():
        idx = 0
        last_cost_unit: float | None = None
        for month in months:
            month_start = month.isoformat()
            month_end = next_month(month)
            while idx < len(moves) and moves[idx][0] < month_end:
                last_cost_unit = moves[idx][1]
                idx += 1
            monthly = month_stats.get((pid, month_start))
            avg_cost_unit = None
            if monthly and monthly["qty"] > 0:
                avg_cost_unit = monthly["value"] / monthly["qty"]
            if last_cost_unit is None and avg_cost_unit is None:
                continue
            out[(pid, month_start)] = {
                "legacy_last_cost_unit": last_cost_unit,
                "legacy_avg_cost_unit": avg_cost_unit if avg_cost_unit is not None else last_cost_unit,
            }
    return out


def fetch_price_history_cost_units(
    conn,
    product_ids,
    from_date: str,
    to_date: str,
) -> dict[tuple[int, str], dict[str, float | None]]:
    """Per (product, month) unit cost from Odoo 12 ``product_price_history`` --
    the cost-over-time table the Inventory Valuation wizard reads (this DB has no
    ``stock_history`` view). Returns {(product_id, 'YYYY-MM-01'): {last, avg}}:

      last_cost_unit = most recent cost with datetime < end-of-month
                       (the wizard's "cost as of date").
      avg_cost_unit  = average cost recorded within the month (None -> caller
                       carries the as-of last cost forward).
    """
    if not product_ids or not table_exists(conn, "product_price_history"):
        return {}
    cols = set(get_table_columns(conn, "product_price_history"))
    if not {"product_id", "datetime", "cost"}.issubset(cols):
        return {}
    start_date = parse_date(from_date)
    end_date = parse_date(to_date)
    if not start_date or not end_date:
        return {}

    months = month_sequence(month_floor(start_date), month_floor(end_date))
    period_end = next_month(month_floor(end_date))  # exclusive upper bound (first day after last month)

    # product_price_history has no (product_id, datetime) index, so do ONE pass:
    # pull the full cost timeline for the sold products up to the period end and
    # bucket per-month in Python (carry-forward for as-of last cost).
    rows = fetch_all(
        conn,
        """
        SELECT product_id, datetime, cost
        FROM product_price_history
        WHERE product_id = ANY(%s) AND datetime < %s
        ORDER BY product_id, datetime
        """,
        (list(product_ids), period_end.isoformat()),
    )

    timeline: dict[int, list[tuple[dt.date, float]]] = defaultdict(list)
    for row in rows:
        pid = to_int(row.get("product_id"), default=0)
        if pid <= 0:
            continue
        ts = row.get("datetime")
        cost = row.get("cost")
        if ts is None or cost is None:
            continue
        ts_date = ts.date() if isinstance(ts, dt.datetime) else ts
        if not isinstance(ts_date, dt.date):
            ts_date = parse_date(str(ts)[:10])
            if not ts_date:
                continue
        timeline[pid].append((ts_date, to_float(cost)))

    out: dict[tuple[int, str], dict[str, float | None]] = {}
    for pid, entries in timeline.items():
        idx = 0
        n = len(entries)
        last_cost: float | None = None
        for month in months:
            month_end = next_month(month)
            month_sum = 0.0
            month_cnt = 0
            while idx < n and entries[idx][0] < month_end:
                last_cost = entries[idx][1]
                if entries[idx][0] >= month:
                    month_sum += entries[idx][1]
                    month_cnt += 1
                idx += 1
            avg_cost = (month_sum / month_cnt) if month_cnt else None
            if last_cost is None and avg_cost is None:
                continue
            out[(pid, month.isoformat())] = {
                "last_cost_unit": last_cost,
                "avg_cost_unit": avg_cost,
            }
    return out


def fetch_stock_move_month_end_valuation(
    conn,
    product_ids,
    from_date: str,
    to_date: str,
) -> dict[tuple[int, str], dict[str, float | None]]:
    """Approximate Odoo12 month-end inventory valuation from stock moves.

    Odoo12 in this DB has no ``stock_valuation_layer`` / ``stock_history`` view.
    For month-end average unit cost we therefore maintain a rolling internal-stock
    quantity/value using stock moves:

    - incoming to internal: add qty * move price_unit
    - outgoing from internal: relieve at current rolling average unit cost

    This is materially closer to the Inventory Valuation wizard's month-end unit
    cost than averaging every ``product_price_history`` update that occurred within
    the month.
    """
    if not product_ids or not table_exists(conn, "stock_move") or not table_exists(conn, "stock_location"):
        return {}

    sm_cols = set(get_table_columns(conn, "stock_move"))
    sl_cols = set(get_table_columns(conn, "stock_location"))
    required = {"product_id", "date", "state", "location_id", "location_dest_id", "price_unit"}
    if not required.issubset(sm_cols) or "usage" not in sl_cols:
        return {}

    qty_col = "quantity_done" if "quantity_done" in sm_cols else ("product_uom_qty" if "product_uom_qty" in sm_cols else ("product_qty" if "product_qty" in sm_cols else None))
    if not qty_col:
        return {}

    start_date = parse_date(from_date)
    end_date = parse_date(to_date)
    if not start_date or not end_date:
        return {}

    months = month_sequence(month_floor(start_date), month_floor(end_date))
    period_end = next_month(month_floor(end_date))

    rows = fetch_all(
        conn,
        f"""
        SELECT
            sm.product_id AS source_product_id,
            sm.date::date AS move_date,
            COALESCE(src.usage, '') AS src_usage,
            COALESCE(dst.usage, '') AS dst_usage,
            ABS(COALESCE(sm.{qty_col}, 0.0)) AS move_qty,
            COALESCE(sm.price_unit, 0.0) AS unit_cost
        FROM stock_move sm
        LEFT JOIN stock_location src ON src.id = sm.location_id
        LEFT JOIN stock_location dst ON dst.id = sm.location_dest_id
        WHERE sm.product_id = ANY(%s)
          AND sm.state = 'done'
          AND sm.date::date < %s::date
          AND (
            src.usage = 'internal'
            OR dst.usage = 'internal'
          )
        ORDER BY sm.product_id, sm.date, sm.id
        """,
        (list(product_ids), period_end.isoformat()),
    )

    moves_by_product: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pid = to_int(row.get("source_product_id"), default=0)
        if pid <= 0:
            continue
        move_date = row.get("move_date")
        if isinstance(move_date, dt.datetime):
            move_date = move_date.date()
        elif not isinstance(move_date, dt.date):
            move_date = parse_date(str(move_date or ""))
        qty = abs(to_float(row.get("move_qty"), default=0.0))
        if not move_date or qty <= 0:
            continue
        moves_by_product[pid].append(
            {
                "move_date": move_date,
                "src_usage": str(row.get("src_usage") or ""),
                "dst_usage": str(row.get("dst_usage") or ""),
                "qty": qty,
                "unit_cost": to_float(row.get("unit_cost"), default=0.0),
            }
        )

    out: dict[tuple[int, str], dict[str, float | None]] = {}
    for pid, moves in moves_by_product.items():
        idx = 0
        qty_on_hand = 0.0
        value_on_hand = 0.0
        last_incoming_cost: float | None = None
        for month in months:
            month_end = next_month(month)
            while idx < len(moves) and moves[idx]["move_date"] < month_end:
                move = moves[idx]
                idx += 1
                src_usage = move["src_usage"]
                dst_usage = move["dst_usage"]
                move_qty = move["qty"]
                move_cost = move["unit_cost"]
                current_avg = (value_on_hand / qty_on_hand) if qty_on_hand else 0.0

                if dst_usage == "internal" and src_usage != "internal":
                    applied_cost = move_cost if move_cost > 0 else current_avg
                    qty_on_hand += move_qty
                    value_on_hand += move_qty * applied_cost
                    if applied_cost > 0:
                        last_incoming_cost = applied_cost
                elif src_usage == "internal" and dst_usage != "internal":
                    applied_cost = current_avg if current_avg > 0 else (move_cost if move_cost > 0 else 0.0)
                    qty_on_hand -= move_qty
                    value_on_hand -= move_qty * applied_cost
                else:
                    # internal -> internal or non-net internal transitions
                    continue

                if abs(qty_on_hand) < 1e-9:
                    qty_on_hand = 0.0
                if abs(value_on_hand) < 1e-9:
                    value_on_hand = 0.0
                if qty_on_hand < 0 and abs(qty_on_hand) < 1e-6:
                    qty_on_hand = 0.0
                if value_on_hand < 0 and abs(value_on_hand) < 1e-4:
                    value_on_hand = 0.0

            avg_cost_unit = (value_on_hand / qty_on_hand) if qty_on_hand else None
            out[(pid, month.isoformat())] = {
                "legacy_stock_close_qty": qty_on_hand,
                "legacy_stock_close_value": value_on_hand,
                "legacy_avg_cost_unit": avg_cost_unit,
                "legacy_last_incoming_cost_unit": last_incoming_cost,
            }
    return out


def fetch_stock_move_cogs_monthly(
    conn,
    from_date: str,
    to_date: str,
) -> dict[tuple[int, str], float]:
    if not table_exists(conn, "stock_move") or not table_exists(conn, "stock_location"):
        return {}
    sm_cols = set(get_table_columns(conn, "stock_move"))
    sl_cols = set(get_table_columns(conn, "stock_location"))
    required = {"product_id", "date", "state", "location_id", "location_dest_id", "price_unit"}
    if not required.issubset(sm_cols) or "usage" not in sl_cols:
        return {}
    qty_col = "quantity_done" if "quantity_done" in sm_cols else ("product_uom_qty" if "product_uom_qty" in sm_cols else ("product_qty" if "product_qty" in sm_cols else None))
    if not qty_col:
        return {}

    rows = fetch_all(
        conn,
        f"""
        SELECT
            sm.product_id AS source_product_id,
            date_trunc('month', sm.date)::date AS period_month,
            SUM(
                CASE
                    WHEN src.usage = 'internal' AND dst.usage = 'customer'
                    THEN ABS(COALESCE(sm.{qty_col}, 0.0) * COALESCE(sm.price_unit, 0.0))
                    WHEN src.usage = 'customer' AND dst.usage = 'internal'
                    THEN -ABS(COALESCE(sm.{qty_col}, 0.0) * COALESCE(sm.price_unit, 0.0))
                    ELSE 0.0
                END
            ) AS legacy_cogs_amount
        FROM stock_move sm
        LEFT JOIN stock_location src ON src.id = sm.location_id
        LEFT JOIN stock_location dst ON dst.id = sm.location_dest_id
        WHERE sm.product_id IS NOT NULL
          AND sm.state = 'done'
          AND sm.date::date >= %s::date
          AND sm.date::date <= %s::date
          AND (
            (src.usage = 'internal' AND dst.usage = 'customer')
            OR (src.usage = 'customer' AND dst.usage = 'internal')
          )
        GROUP BY sm.product_id, date_trunc('month', sm.date)::date
        """,
        (from_date, to_date),
    )
    out: dict[tuple[int, str], float] = {}
    for row in rows:
        pid = to_int(row.get("source_product_id"), default=0)
        month = str(row.get("period_month") or "")[:10]
        if pid <= 0 or not month:
            continue
        out[(pid, month)] = to_float(row.get("legacy_cogs_amount"), default=0.0)
    return out


def fetch_sales_monthly(
    conn,
    from_date: str,
    to_date: str,
    ai_cols: set[str],
    ail_cols: set[str],
    pp_cols: set[str],
) -> list[dict[str, Any]]:
    if "id" not in ai_cols or "invoice_id" not in ail_cols or "product_id" not in ail_cols:
        fail("Required account_invoice/account_invoice_line keys are missing for sales extraction.")
    if "type" not in ai_cols:
        fail("Column account_invoice.type is required for signed invoice logic.")

    date_col = None
    for candidate in ("date_invoice", "invoice_date", "date"):
        if candidate in ai_cols:
            date_col = candidate
            break
    if not date_col:
        fail("No invoice date column found in account_invoice (expected date_invoice/invoice_date/date).")

    qty_col = "quantity" if "quantity" in ail_cols else ("qty" if "qty" in ail_cols else None)
    amount_expr = None
    sales_untaxed_total_expr = None
    total_amount_expr = None
    sales_total_expr = None
    discount_reason_total_expr = None
    if "price_subtotal_signed" in ail_cols:
        # Match Odoo12 Invoice Analysis, which uses account_invoice_line.price_subtotal_signed.
        amount_expr = "COALESCE(ail.price_subtotal_signed, 0.0)"
        sales_untaxed_total_expr = "ABS(COALESCE(ail.price_subtotal_signed, 0.0))"
    else:
        amount_col = "price_subtotal" if "price_subtotal" in ail_cols else ("price_total" if "price_total" in ail_cols else None)
        if amount_col:
            amount_expr = (
                f"COALESCE(ail.{amount_col}, 0.0)"
                " * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END"
            )
            sales_untaxed_total_expr = f"ABS(COALESCE(ail.{amount_col}, 0.0))"
    if "price_total" in ail_cols:
        total_amount_expr = (
            "COALESCE(ail.price_total, 0.0)"
            " * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END"
        )
        sales_total_expr = "ABS(COALESCE(ail.price_total, 0.0))"
    elif amount_expr and sales_untaxed_total_expr:
        total_amount_expr = amount_expr
        sales_total_expr = sales_untaxed_total_expr

    report_total_expr, report_total_source = detect_legacy_report_total_source(conn)
    join_air = ""
    if report_total_expr:
        total_amount_expr = (
            f"COALESCE({report_total_expr}, 0.0)"
            " * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END"
        )
        sales_total_expr = f"ABS(COALESCE({report_total_expr}, 0.0))"
        discount_reason_total_expr = f"ABS(COALESCE({report_total_expr}, 0.0))"
        join_air = "LEFT JOIN account_invoice_report air ON air.id = ail.id"
    elif sales_total_expr:
        discount_reason_total_expr = sales_total_expr

    if not qty_col or not amount_expr or not sales_untaxed_total_expr or not total_amount_expr or not sales_total_expr:
        fail("Missing quantity/amount columns on account_invoice_line.")

    state_filter_sql = ""
    if "state" in ai_cols:
        state_filter_sql = "AND ai.state NOT IN ('draft', 'cancel')"

    if "reason_id" in ail_cols and "reason_id" in ai_cols:
        discount_reason_expr = "COALESCE(ail.reason_id, ai.reason_id)"
    elif "reason_id" in ail_cols:
        discount_reason_expr = "ail.reason_id"
    elif "reason_id" in ai_cols:
        discount_reason_expr = "ai.reason_id"
    else:
        discount_reason_expr = "NULL::integer"

    cost_expr, cost_source = detect_legacy_cost_source(ail_cols, pp_cols)
    has_line_total_cost = cost_expr == "ail.total_cost"
    if cost_expr:
        if has_line_total_cost:
            cogs_expr = (
                "COALESCE(ail.total_cost, 0.0)"
                " * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END"
            )
        else:
            cogs_expr = (
                f"COALESCE({cost_expr}, 0.0) * COALESCE(ail.{qty_col}, 0.0)"
                " * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END"
            )
        cogs_sum_sql = f"SUM({cogs_expr}) AS legacy_cogs_amount,"
        cost_flag_sql = "TRUE AS legacy_cost_available,"
        cost_source_sql = f"'{cost_source}'::text AS legacy_cost_source,"
    else:
        cogs_sum_sql = "NULL::double precision AS legacy_cogs_amount,"
        cost_flag_sql = "FALSE AS legacy_cost_available,"
        cost_source_sql = "NULL::text AS legacy_cost_source,"

    rows = fetch_all(
        conn,
        f"""
        SELECT
            ail.product_id AS source_product_id,
            date_trunc('month', ai.{date_col})::date AS period_month,
            SUM(
                COALESCE(ail.{qty_col}, 0.0)
                * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END
            ) AS legacy_sales_qty,
            SUM(
                {amount_expr}
            ) AS legacy_sales_amount
            ,
            SUM(
                CASE
                    WHEN ai.type = 'out_invoice'
                    THEN {sales_untaxed_total_expr}
                    ELSE 0.0
                END
            ) AS legacy_untaxed_total_sales_amount,
            SUM(
                CASE
                    WHEN ai.type = 'out_invoice'
                    THEN {sales_total_expr}
                    ELSE 0.0
                END
            ) AS legacy_total_sales_amount,
            SUM(
                {total_amount_expr}
            ) AS legacy_net_total_sales_amount,
            SUM(
                CASE
                    WHEN ai.type IN ('out_refund', 'in_refund')
                    THEN -ABS(COALESCE(ail.{qty_col}, 0.0))
                    ELSE 0.0
                END
            ) AS legacy_return_qty,
            SUM(
                CASE
                    WHEN ai.type IN ('out_refund', 'in_refund')
                    THEN -ABS({sales_untaxed_total_expr})
                    ELSE 0.0
                END
            ) AS legacy_return_amount,
            SUM(
                CASE
                    WHEN ai.type IN ('out_refund', 'in_refund')
                    THEN -ABS({sales_total_expr})
                    ELSE 0.0
                END
            ) AS legacy_return_total_amount,
            SUM(
                CASE
                    WHEN ai.type = 'out_invoice' AND {discount_reason_expr} IS NOT NULL
                    THEN {sales_untaxed_total_expr}
                    ELSE 0.0
                END
            ) AS legacy_discount_reason_sales_untaxed,
            SUM(
                CASE
                    WHEN ai.type = 'out_invoice' AND {discount_reason_expr} IS NOT NULL
                    THEN {discount_reason_total_expr}
                    ELSE 0.0
                END
            ) AS legacy_discount_reason_sales_total,
            SUM(
                COALESCE(ail.{qty_col}, 0.0) * COALESCE(ail.price_unit, 0.0) * (COALESCE(ail.discount, 0.0) / 100.0)
                * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END
            ) AS legacy_discount_amount,
            SUM(
                COALESCE(ail.{qty_col}, 0.0) * COALESCE(ail.price_unit, 0.0)
                * CASE WHEN ai.type IN ('out_refund', 'in_refund') THEN -1 ELSE 1 END
            ) AS legacy_gross_sales_amount,
            {cogs_sum_sql}
            {cost_flag_sql}
            {cost_source_sql}
            TRUE AS legacy_margin_comparable
        FROM account_invoice_line ail
        JOIN account_invoice ai
            ON ai.id = ail.invoice_id
        {join_air}
        LEFT JOIN product_product pp
            ON pp.id = ail.product_id
        WHERE ail.product_id IS NOT NULL
          AND ai.{date_col}::date >= %s::date
          AND ai.{date_col}::date <= %s::date
          AND ai.type IN ('out_invoice', 'out_refund')
          {state_filter_sql}
        GROUP BY ail.product_id, date_trunc('month', ai.{date_col})::date
        ORDER BY ail.product_id, period_month
        """,
        (from_date, to_date),
    )
    if not cost_expr:
        for row in rows:
            row["legacy_margin_comparable"] = False
    return rows


def fetch_stock_monthly(
    conn,
    start_month: dt.date,
    end_month: dt.date,
) -> tuple[list[dict[str, Any]], bool]:
    if not table_exists(conn, "stock_valuation_layer"):
        return [], False

    svl_cols = set(get_table_columns(conn, "stock_valuation_layer"))
    required = {"product_id", "quantity", "value", "create_date"}
    if not required.issubset(svl_cols):
        return [], False

    rows = fetch_all(
        conn,
        """
        WITH months AS (
            SELECT generate_series(%s::date, %s::date, INTERVAL '1 month')::date AS period_month
        ),
        products AS (
            SELECT DISTINCT svl.product_id AS source_product_id
            FROM stock_valuation_layer svl
            WHERE svl.product_id IS NOT NULL
              AND svl.create_date < (%s::date + INTERVAL '1 month')
        ),
        opening AS (
            SELECT
                svl.product_id AS source_product_id,
                SUM(COALESCE(svl.quantity, 0.0)) AS qty_open,
                SUM(COALESCE(svl.value, 0.0)) AS value_open
            FROM stock_valuation_layer svl
            WHERE svl.product_id IS NOT NULL
              AND svl.create_date < %s::date
            GROUP BY svl.product_id
        ),
        month_delta AS (
            SELECT
                svl.product_id AS source_product_id,
                date_trunc('month', svl.create_date)::date AS period_month,
                SUM(COALESCE(svl.quantity, 0.0)) AS qty_delta,
                SUM(COALESCE(svl.value, 0.0)) AS value_delta
            FROM stock_valuation_layer svl
            WHERE svl.product_id IS NOT NULL
              AND svl.create_date >= %s::date
              AND svl.create_date < (%s::date + INTERVAL '1 month')
            GROUP BY svl.product_id, date_trunc('month', svl.create_date)::date
        ),
        grid AS (
            SELECT
                p.source_product_id,
                m.period_month,
                COALESCE(o.qty_open, 0.0) AS qty_open,
                COALESCE(o.value_open, 0.0) AS value_open,
                COALESCE(md.qty_delta, 0.0) AS qty_delta,
                COALESCE(md.value_delta, 0.0) AS value_delta
            FROM products p
            CROSS JOIN months m
            LEFT JOIN opening o
                ON o.source_product_id = p.source_product_id
            LEFT JOIN month_delta md
                ON md.source_product_id = p.source_product_id
               AND md.period_month = m.period_month
        )
        SELECT
            source_product_id,
            period_month,
            qty_open + SUM(qty_delta) OVER (
                PARTITION BY source_product_id
                ORDER BY period_month
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS legacy_stock_close_qty,
            value_open + SUM(value_delta) OVER (
                PARTITION BY source_product_id
                ORDER BY period_month
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS legacy_stock_close_value,
            TRUE AS value_available
        FROM grid
        ORDER BY source_product_id, period_month
        """,
        (
            start_month.isoformat(),
            end_month.isoformat(),
            end_month.isoformat(),
            start_month.isoformat(),
            start_month.isoformat(),
            end_month.isoformat(),
        ),
    )
    return rows, True


def fetch_product_metadata(
    conn,
    product_ids: list[int],
    pp_cols: set[str],
    pt_cols: set[str],
) -> dict[int, dict[str, Any]]:
    if not product_ids:
        return {}

    has_pt = "product_tmpl_id" in pp_cols and table_exists(conn, "product_template")
    join_pt = "LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id" if has_pt else ""

    has_pc = has_pt and "categ_id" in pt_cols and table_exists(conn, "product_category")
    pc_cols = set(get_table_columns(conn, "product_category")) if has_pc else set()
    join_pc = "LEFT JOIN product_category pc ON pc.id = pt.categ_id" if has_pc and "name" in pc_cols else ""
    category_name_expr = "pc.name" if join_pc else "NULL::text"

    brand_fk, brand_table, has_brand = detect_brand_join(conn, pt_cols)
    join_brand = ""
    brand_name_expr = "NULL::text"
    if has_pt and has_brand and brand_fk and brand_table:
        join_brand = f"LEFT JOIN {brand_table} pb ON pb.id = pt.{brand_fk}"
        brand_name_expr = "pb.name"

    default_code_expr = "pp.default_code" if "default_code" in pp_cols else "NULL::text"
    barcode_expr = "pp.barcode" if "barcode" in pp_cols else "NULL::text"
    if has_pt and "name" in pt_cols:
        name_expr = "pt.name"
    elif "name_template" in pp_cols:
        name_expr = "pp.name_template"
    elif "default_code" in pp_cols:
        name_expr = "pp.default_code"
    else:
        name_expr = "NULL::text"

    rows = fetch_all(
        conn,
        f"""
        SELECT
            pp.id AS source_product_id,
            {default_code_expr} AS source_default_code,
            {barcode_expr} AS source_barcode,
            {name_expr} AS source_name,
            {category_name_expr} AS source_category_name,
            {brand_name_expr} AS source_brand_name
        FROM product_product pp
        {join_pt}
        {join_pc}
        {join_brand}
        WHERE pp.id = ANY(%s)
        """,
        (product_ids,),
    )
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        pid = to_int(row.get("source_product_id"), default=0)
        if pid <= 0:
            continue
        out[pid] = {
            "source_default_code": row.get("source_default_code"),
            "source_barcode": row.get("source_barcode"),
            "source_name": row.get("source_name"),
            "source_category_name": row.get("source_category_name"),
            "source_brand_name": row.get("source_brand_name"),
        }
    return out


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    state_file = Path(args.state_file)
    ensure_dir(out_dir)
    ensure_dir(state_file.parent)

    from_date = parse_date(args.from_date)
    to_date = parse_date(args.to_date)
    if not from_date or not to_date:
        fail("Invalid from/to date. Expected YYYY-MM-DD.")
    if from_date > to_date:
        fail("--from-date must be <= --to-date.")

    start_month = month_floor(from_date)
    end_month = month_floor(to_date)
    months = month_sequence(start_month, end_month)

    out_file = out_dir / "legacy_product_month_fact_raw.csv"
    summary_file = out_dir / "extract_product_compare_summary.json"

    state = load_state(state_file) if args.resume else {}
    if args.resume and state.get("status") == "completed" and out_file.exists():
        log("Extract already completed; nothing to do.")
        return

    state.setdefault("steps", {})
    state["status"] = "running"
    save_state(state_file, state)

    conn = connect_from_env(
        args.source_env,
        dbname=args.source_db,
        readonly=True,
        statement_timeout_ms=args.statement_timeout_ms if args.statement_timeout_ms > 0 else None,
    )
    try:
        if not table_exists(conn, "account_invoice") or not table_exists(conn, "account_invoice_line"):
            fail("Source DB does not contain account_invoice/account_invoice_line tables.")
        if not table_exists(conn, "product_product"):
            fail("Source DB does not contain product_product table.")

        ai_cols = set(get_table_columns(conn, "account_invoice"))
        ail_cols = set(get_table_columns(conn, "account_invoice_line"))
        pp_cols = set(get_table_columns(conn, "product_product"))
        pt_cols = set(get_table_columns(conn, "product_template")) if table_exists(conn, "product_template") else set()

        log("Extracting legacy product monthly sales (2025 scope)")
        sales_rows = fetch_sales_monthly(conn, args.from_date, args.to_date, ai_cols, ail_cols, pp_cols)
        sales_map: dict[tuple[int, str], dict[str, Any]] = {}
        for row in sales_rows:
            pid = to_int(row.get("source_product_id"), default=0)
            month = str(row.get("period_month") or "")[:10]
            if pid <= 0 or not month:
                continue
            sales_qty = to_float(row.get("legacy_sales_qty"), default=0.0)
            sales_amount = to_float(row.get("legacy_sales_amount"), default=0.0)
            cogs_amount = row.get("legacy_cogs_amount")
            cogs_amount_f = to_float(cogs_amount, default=0.0) if cogs_amount is not None else None
            margin_amount = (sales_amount - cogs_amount_f) if cogs_amount_f is not None else None
            margin_pct = ((margin_amount / sales_amount) * 100.0) if (margin_amount is not None and sales_amount) else None
            legacy_cost_available = bool(row.get("legacy_cost_available")) and cogs_amount is not None
            legacy_margin_comparable = bool(row.get("legacy_margin_comparable")) and legacy_cost_available
            sales_map[(pid, month)] = {
                "legacy_sales_qty": sales_qty,
                "legacy_sales_amount": sales_amount,
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
                "legacy_net_sales_amount": sales_amount,
                "legacy_asp": (sales_amount / sales_qty) if sales_qty else None,
                "legacy_cogs_amount": cogs_amount_f,
                "legacy_margin_amount": margin_amount,
                "legacy_margin_pct": margin_pct,
                "legacy_cost_available": legacy_cost_available,
                "legacy_cost_source": row.get("legacy_cost_source"),
                "legacy_margin_comparable": legacy_margin_comparable,
                "legacy_last_cost_unit": None,
                "legacy_avg_cost_unit": None,
            }

        log("Extracting legacy month-end inventory valuation from stock moves")
        sold_pids = sorted({pid for (pid, _m) in sales_map.keys()})
        stock_move_valuation = fetch_stock_move_month_end_valuation(conn, sold_pids, args.from_date, args.to_date)
        if stock_move_valuation:
            applied_move_valuation = 0
            for key, vals in sales_map.items():
                mv = stock_move_valuation.get(key)
                if not mv:
                    continue
                close_qty = mv.get("legacy_stock_close_qty")
                close_value = mv.get("legacy_stock_close_value")
                avg_cost_unit = mv.get("legacy_avg_cost_unit")
                if close_qty is not None:
                    vals["legacy_stock_close_qty"] = float(close_qty)
                if close_value is not None:
                    vals["legacy_stock_close_value"] = float(close_value)
                    vals["value_available"] = True
                if avg_cost_unit is not None:
                    vals["legacy_avg_cost_unit"] = float(avg_cost_unit)
                    applied_move_valuation += 1
            log("Applied stock-move month-end valuation to %d (product, month) rows" % applied_move_valuation)

        log("Extracting legacy unit costs from Odoo 12 product_price_history (Inventory Valuation wizard basis)")
        price_units = fetch_price_history_cost_units(conn, sold_pids, args.from_date, args.to_date)
        if price_units:
            applied_ph = 0
            for key, vals in sales_map.items():
                cu = price_units.get(key)
                if not cu:
                    continue
                last_cost_unit = cu.get("last_cost_unit")
                avg_cost_unit = cu.get("avg_cost_unit")
                if last_cost_unit is None and avg_cost_unit is None:
                    continue
                if last_cost_unit is None:
                    last_cost_unit = avg_cost_unit
                if avg_cost_unit is None:
                    avg_cost_unit = last_cost_unit
                sales_qty = to_float(vals.get("legacy_sales_qty"), default=0.0)
                sales_amount = to_float(vals.get("legacy_sales_amount"), default=0.0)  # untaxed net sales
                # Replicate the Odoo 18 current-side Net Margin: untaxed_cost = cost / 1.14.
                untaxed_cost_unit = float(last_cost_unit) / UNTAX_COST_DIVISOR
                cogs_val = sales_qty * untaxed_cost_unit
                margin_amount = sales_amount - cogs_val
                vals["legacy_cogs_amount"] = cogs_val
                vals["legacy_margin_amount"] = margin_amount
                vals["legacy_margin_pct"] = (margin_amount / sales_amount) * 100.0 if sales_amount else None
                vals["legacy_cost_available"] = True
                vals["legacy_cost_source"] = "product_price_history.last_cost/%.2f" % UNTAX_COST_DIVISOR
                vals["legacy_margin_comparable"] = True
                vals["legacy_last_cost_unit"] = float(last_cost_unit)
                # Keep avg unit tied to month-end valuation if we have it; only
                # fall back to price_history's intra-month average when nothing
                # better is available.
                if vals.get("legacy_avg_cost_unit") in (None, ""):
                    vals["legacy_avg_cost_unit"] = float(avg_cost_unit)
                applied_ph += 1
            log("Applied product_price_history valuation COGS/margin to %d (product, month) rows" % applied_ph)
        else:
            log("product_price_history unavailable; relying on stock_move valuation fallback")

        log("Filling any remaining unit costs from stock moves (supplier -> internal)")
        valuation_units = fetch_stock_move_valuation_units(conn, args.from_date, args.to_date)
        if valuation_units:
            applied_valuation = False
            for key, vals in sales_map.items():
                if vals.get("legacy_cost_available"):
                    continue
                cost_units = valuation_units.get(key)
                if not cost_units:
                    continue
                last_cost_unit = cost_units.get("legacy_last_cost_unit")
                avg_cost_unit = cost_units.get("legacy_avg_cost_unit")
                if last_cost_unit is None:
                    vals["legacy_avg_cost_unit"] = avg_cost_unit
                    continue
                sales_qty = to_float(vals.get("legacy_sales_qty"), default=0.0)
                sales_amount = to_float(vals.get("legacy_sales_amount"), default=0.0)
                cogs_val = sales_qty * float(last_cost_unit)
                margin_amount = sales_amount - cogs_val
                vals["legacy_cogs_amount"] = cogs_val
                vals["legacy_margin_amount"] = margin_amount
                vals["legacy_margin_pct"] = (margin_amount / sales_amount) * 100.0 if sales_amount else None
                vals["legacy_cost_available"] = True
                vals["legacy_cost_source"] = "stock_move.supplier_internal.last_cost_unit"
                vals["legacy_margin_comparable"] = True
                vals["legacy_last_cost_unit"] = float(last_cost_unit)
                vals["legacy_avg_cost_unit"] = float(avg_cost_unit) if avg_cost_unit is not None else float(last_cost_unit)
                applied_valuation = True
            if applied_valuation:
                log("Applied valuation-style last-cost legacy COGS/margin")
            else:
                log("Valuation-style unit costs found, but none aligned to extracted sales months")

        # Final fallback for DBs without usable valuation-style unit costs.
        if not any(bool(v.get("legacy_cost_available")) for v in sales_map.values()):
            log("Valuation unit costs unavailable; attempting stock_move internal/customer fallback")
            fallback_cogs = fetch_stock_move_cogs_monthly(conn, args.from_date, args.to_date)
            if fallback_cogs:
                for key, vals in sales_map.items():
                    cogs_val = fallback_cogs.get(key)
                    if cogs_val is None:
                        continue
                    sales_amount = to_float(vals.get("legacy_sales_amount"), default=0.0)
                    margin_amount = sales_amount - cogs_val
                    vals["legacy_cogs_amount"] = cogs_val
                    vals["legacy_margin_amount"] = margin_amount
                    vals["legacy_margin_pct"] = (margin_amount / sales_amount) * 100.0 if sales_amount else None
                    vals["legacy_cost_available"] = True
                    vals["legacy_cost_source"] = "stock_move.price_unit_x_qty_internal_customer"
                    vals["legacy_margin_comparable"] = True
                log("Applied stock_move internal/customer fallback legacy COGS/margin")
            else:
                log("No stock_move fallback COGS data available for legacy margin extraction")

        log("Extracting legacy product monthly stock close (valuation-based)")
        stock_rows, stock_available = fetch_stock_monthly(conn, start_month, end_month)
        stock_source_label = "stock_valuation_layer" if stock_available else ("stock_move_month_end_valuation" if stock_move_valuation else "unavailable")
        stock_map: dict[tuple[int, str], dict[str, Any]] = {}
        for row in stock_rows:
            pid = to_int(row.get("source_product_id"), default=0)
            month = str(row.get("period_month") or "")[:10]
            if pid <= 0 or not month:
                continue
            stock_map[(pid, month)] = {
                "legacy_stock_close_qty": to_float(row.get("legacy_stock_close_qty"), default=0.0),
                "legacy_stock_close_value": to_float(row.get("legacy_stock_close_value"), default=0.0),
                "value_available": bool(row.get("value_available")),
            }
        if not stock_available and stock_move_valuation:
            for key, row in stock_move_valuation.items():
                stock_map[key] = {
                    "legacy_stock_close_qty": to_float(row.get("legacy_stock_close_qty"), default=0.0),
                    "legacy_stock_close_value": to_float(row.get("legacy_stock_close_value"), default=0.0),
                    "value_available": row.get("legacy_stock_close_value") is not None,
                }
            stock_available = bool(stock_map)

        product_ids = sorted({pid for pid, _ in sales_map.keys()} | {pid for pid, _ in stock_map.keys()})
        log(f"Building product metadata for {len(product_ids)} products")
        metadata = fetch_product_metadata(conn, product_ids, pp_cols, pt_cols)

        rows_out: list[dict[str, Any]] = []
        for pid in product_ids:
            meta = metadata.get(pid, {})
            for month in months:
                month_key = month.isoformat()
                sales = sales_map.get((pid, month_key), {})
                stock = stock_map.get((pid, month_key))
                rows_out.append(
                    {
                        "source_db": args.source_db,
                        "source_product_id": pid,
                        "period_month": month_key,
                        "warehouse_key": "all",
                        "source_default_code": meta.get("source_default_code"),
                        "source_barcode": meta.get("source_barcode"),
                        "source_name": meta.get("source_name"),
                        "source_category_name": meta.get("source_category_name"),
                        "source_brand_name": meta.get("source_brand_name"),
                        "legacy_sales_qty": to_float(sales.get("legacy_sales_qty"), default=0.0),
                        "legacy_sales_amount": to_float(sales.get("legacy_sales_amount"), default=0.0),
                        "legacy_untaxed_total_sales_amount": to_float(sales.get("legacy_untaxed_total_sales_amount"), default=0.0),
                        "legacy_total_sales_amount": to_float(sales.get("legacy_total_sales_amount"), default=0.0),
                        "legacy_net_total_sales_amount": to_float(sales.get("legacy_net_total_sales_amount"), default=0.0),
                        "legacy_return_qty": to_float(sales.get("legacy_return_qty"), default=0.0),
                        "legacy_return_amount": to_float(sales.get("legacy_return_amount"), default=0.0),
                        "legacy_return_total_amount": to_float(sales.get("legacy_return_total_amount"), default=0.0),
                        "legacy_discount_reason_sales_untaxed": to_float(sales.get("legacy_discount_reason_sales_untaxed"), default=0.0),
                        "legacy_discount_reason_sales_total": to_float(sales.get("legacy_discount_reason_sales_total"), default=0.0),
                        "legacy_discount_amount": to_float(sales.get("legacy_discount_amount"), default=0.0),
                        "legacy_gross_sales_amount": to_float(sales.get("legacy_gross_sales_amount"), default=0.0),
                        "legacy_net_sales_amount": to_float(sales.get("legacy_net_sales_amount"), default=0.0),
                        "legacy_asp": sales.get("legacy_asp"),
                        "legacy_cogs_amount": sales.get("legacy_cogs_amount"),
                        "legacy_margin_amount": sales.get("legacy_margin_amount"),
                        "legacy_margin_pct": sales.get("legacy_margin_pct"),
                        "legacy_last_cost_unit": sales.get("legacy_last_cost_unit"),
                        "legacy_avg_cost_unit": sales.get("legacy_avg_cost_unit"),
                        "legacy_cost_available": bool(sales.get("legacy_cost_available")),
                        "legacy_cost_source": sales.get("legacy_cost_source"),
                        "legacy_margin_comparable": bool(sales.get("legacy_margin_comparable")),
                        "legacy_stock_close_qty": stock.get("legacy_stock_close_qty") if stock else None,
                        "legacy_stock_close_value": stock.get("legacy_stock_close_value") if stock else None,
                        "value_available": bool(stock.get("value_available")) if stock else False,
                        "legacy_payload": json.dumps(
                            {
                                "sales_present": (pid, month_key) in sales_map,
                                "stock_present": (pid, month_key) in stock_map,
                                "stock_source": stock_source_label,
                                "legacy_cost_available": bool(sales.get("legacy_cost_available")),
                                "legacy_cost_source": sales.get("legacy_cost_source"),
                                "legacy_last_cost_unit": sales.get("legacy_last_cost_unit"),
                                "legacy_avg_cost_unit": sales.get("legacy_avg_cost_unit"),
                                "legacy_margin_comparable": bool(sales.get("legacy_margin_comparable")),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                )

        write_csv(
            out_file,
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
                "legacy_payload",
            ],
            mode="w",
        )

        totals_by_month: dict[str, dict[str, float]] = {}
        for row in rows_out:
            month = str(row.get("period_month") or "")
            agg = totals_by_month.setdefault(
                month,
                {
                    "legacy_sales_qty": 0.0,
                    "legacy_sales_amount": 0.0,
                    "legacy_untaxed_total_sales_amount": 0.0,
                    "legacy_total_sales_amount": 0.0,
                    "legacy_net_total_sales_amount": 0.0,
                    "legacy_return_qty": 0.0,
                    "legacy_return_amount": 0.0,
                    "legacy_return_total_amount": 0.0,
                    "legacy_discount_reason_sales_untaxed": 0.0,
                    "legacy_discount_reason_sales_total": 0.0,
                    "legacy_discount_amount": 0.0,
                    "legacy_gross_sales_amount": 0.0,
                    "legacy_net_sales_amount": 0.0,
                },
            )
            agg["legacy_sales_qty"] += to_float(row.get("legacy_sales_qty"), default=0.0)
            agg["legacy_sales_amount"] += to_float(row.get("legacy_sales_amount"), default=0.0)
            agg["legacy_untaxed_total_sales_amount"] += to_float(row.get("legacy_untaxed_total_sales_amount"), default=0.0)
            agg["legacy_total_sales_amount"] += to_float(row.get("legacy_total_sales_amount"), default=0.0)
            agg["legacy_net_total_sales_amount"] += to_float(row.get("legacy_net_total_sales_amount"), default=0.0)
            agg["legacy_return_qty"] += to_float(row.get("legacy_return_qty"), default=0.0)
            agg["legacy_return_amount"] += to_float(row.get("legacy_return_amount"), default=0.0)
            agg["legacy_return_total_amount"] += to_float(row.get("legacy_return_total_amount"), default=0.0)
            agg["legacy_discount_reason_sales_untaxed"] += to_float(row.get("legacy_discount_reason_sales_untaxed"), default=0.0)
            agg["legacy_discount_reason_sales_total"] += to_float(row.get("legacy_discount_reason_sales_total"), default=0.0)
            agg["legacy_discount_amount"] += to_float(row.get("legacy_discount_amount"), default=0.0)
            agg["legacy_gross_sales_amount"] += to_float(row.get("legacy_gross_sales_amount"), default=0.0)
            agg["legacy_net_sales_amount"] += to_float(row.get("legacy_net_sales_amount"), default=0.0)

        summary = {
            "source_db": args.source_db,
            "from_date": args.from_date,
            "to_date": args.to_date,
            "months": [m.isoformat() for m in months],
            "products": len(product_ids),
            "rows": len(rows_out),
            "stock_valuation_available": stock_available,
            "month_totals": totals_by_month,
            "output_file": str(out_file),
        }
        summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        state["steps"]["extract"] = {
            "products": len(product_ids),
            "rows": len(rows_out),
            "stock_valuation_available": stock_available,
            "output_file": str(out_file),
        }
        state["status"] = "completed"
        save_state(state_file, state)
        log(f"Extraction completed: {summary}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

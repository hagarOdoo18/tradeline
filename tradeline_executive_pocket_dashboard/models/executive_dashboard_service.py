from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class ExecutiveDashboardService(models.AbstractModel):
    _name = "tradeline.executive.dashboard.service"
    _description = "Executive Dashboard Service"

    FX_PERIOD_DAYS = {
        "1D": 1,
        "1M": 30,
        "3M": 90,
        "6M": 180,
        "1Y": 365,
    }
    FX_TARGETS = {
        "USD/EGP": {"source_symbol": "USDEGP=X", "invert": False},
        "EUR/EGP": {"source_symbol": "EUREGP=X", "invert": False},
        "GBP/EGP": {"source_symbol": "GBPEGP=X", "invert": False},
    }
    DRILL_CATALOG = {
        "finance": {
            "label": "Finance",
            "description": "Revenue, collections, receivables, and margin quality.",
            "groups": {"company": "Company", "branch": "Branch", "customer": "Customer", "payment_state": "Payment State"},
            "metrics": {
                "net_revenue": "Net Revenue",
                "net_margin": "Net Margin",
                "margin_pct": "Margin %",
                "credit_note_value": "Credit Note Value",
                "invoice_count": "Invoice Count",
            },
            "default_group": "branch",
            "default_metric": "net_revenue",
        },
        "sales": {
            "label": "Sales",
            "description": "Performance, mix, and margin breakdown.",
            "groups": {
                "company": "Company",
                "branch": "Branch",
                "salesperson": "Salesperson",
                "customer": "Customer",
                "category": "Category",
                "product": "Product",
            },
            "metrics": {
                "net_revenue": "Net Revenue",
                "net_margin": "Net Margin",
                "margin_pct": "Margin %",
                "average_basket": "Average Basket",
                "invoice_count": "Invoice Count",
            },
            "default_group": "branch",
            "default_metric": "net_revenue",
        },
        "inventory": {
            "label": "Inventory",
            "description": "Inventory cost exposure by product/category/company.",
            "groups": {"category": "Category", "company": "Company", "product": "Product"},
            "metrics": {
                "allocated_value": "Allocated Inventory Cost",
                "on_hand_qty": "On Hand Qty",
                "unit_cost": "Unit Cost",
            },
            "default_group": "category",
            "default_metric": "allocated_value",
        },
        "pipeline": {
            "label": "Pipeline",
            "description": "Open and weighted opportunity pipeline.",
            "groups": {"company": "Company", "stage": "Stage", "owner": "Owner", "branch": "Branch"},
            "metrics": {
                "weighted_pipeline": "Weighted Pipeline",
                "open_pipeline": "Open Pipeline",
                "open_opportunities": "Open Opportunities",
            },
            "default_group": "stage",
            "default_metric": "weighted_pipeline",
        },
    }

    def _dictfetchall(self):
        columns = [desc[0] for desc in self.env.cr.description]
        return [dict(zip(columns, row)) for row in self.env.cr.fetchall()]

    def _dictfetchone(self):
        row = self.env.cr.fetchone()
        if not row:
            return {}
        columns = [desc[0] for desc in self.env.cr.description]
        return dict(zip(columns, row))

    def _has_table(self, table_name: str) -> bool:
        self.env.cr.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            )
            """,
            (table_name,),
        )
        return bool(self.env.cr.fetchone()[0])

    def _has_column(self, table_name: str, column_name: str) -> bool:
        self.env.cr.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            )
            """,
            (table_name, column_name),
        )
        return bool(self.env.cr.fetchone()[0])

    def _parse_date(self, value, fallback: date) -> date:
        if not value:
            return fallback
        if isinstance(value, date):
            return value
        try:
            return fields.Date.to_date(value)
        except Exception:
            return fallback

    def _pick_translated_value(self, mapping: dict) -> str:
        if not mapping:
            return ""
        user_lang = self.env.user.lang or "en_US"
        probes = [user_lang]
        if "_" in user_lang:
            probes.append(user_lang.split("_")[0])
        probes.extend(["en_US", "en", "ar_001", "ar"])
        for key in probes:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in mapping.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _clean_dimension_label(self, value):
        if value is None:
            return "Unspecified"
        if isinstance(value, dict):
            cleaned = self._pick_translated_value(value)
            return cleaned or "Unspecified"
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    return value
                if isinstance(parsed, dict):
                    cleaned = self._pick_translated_value(parsed)
                    return cleaned or value
            return value
        return value

    def _ensure_exec_admin(self):
        if not self.env.user.has_group("tradeline_executive_pocket_dashboard.group_exec_admin"):
            raise AccessError("Executive Pocket Dashboard is restricted to Administrator.")

    def _resolve_filter_scope(self, filters: dict | None) -> dict:
        filters = filters or {}
        today = fields.Date.context_today(self)
        default_start = today.replace(day=1)

        start_date = self._parse_date(filters.get("start_date"), default_start)
        end_date = self._parse_date(filters.get("end_date"), today)
        report_date = self._parse_date(filters.get("report_date"), today)
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        company_ids = [int(x) for x in (filters.get("company_ids") or []) if x]
        branch_ids = [int(x) for x in (filters.get("branch_ids") or []) if x]
        salesperson_ids = [int(x) for x in (filters.get("salesperson_ids") or []) if x]

        user_company_ids = self.env.user.company_ids.ids
        if not company_ids:
            company_ids = user_company_ids
        if not self.env.user.has_group("tradeline_executive_pocket_dashboard.group_exec_admin"):
            company_ids = [x for x in company_ids if x in user_company_ids]

        user_branch_ids = []
        if "branch_ids" in self.env.user._fields:
            user_branch_ids = self.env.user.branch_ids.ids
        if branch_ids and user_branch_ids and not self.env.user.has_group(
            "tradeline_executive_pocket_dashboard.group_exec_admin"
        ):
            branch_ids = [x for x in branch_ids if x in user_branch_ids]
        elif not branch_ids and user_branch_ids:
            branch_ids = user_branch_ids

        return {
            "start_date": start_date,
            "end_date": end_date,
            "report_date": report_date,
            "company_ids": company_ids,
            "branch_ids": branch_ids,
            "salesperson_ids": salesperson_ids,
        }

    def _build_scope_clause(
        self,
        *,
        alias: str,
        table_name: str,
        filters: dict,
        include_sales_rep: bool = False,
        sales_rep_field: str = "sales_rep_id",
    ) -> tuple[str, list]:
        clauses = ["1=1"]
        params = []

        if filters["company_ids"] and self._has_column(table_name, "company_id"):
            clauses.append(f"{alias}.company_id = ANY(%s)")
            params.append(filters["company_ids"])

        if filters["branch_ids"] and self._has_column(table_name, "branch_id"):
            clauses.append(f"{alias}.branch_id = ANY(%s)")
            params.append(filters["branch_ids"])

        if include_sales_rep and filters["salesperson_ids"] and self._has_column(table_name, sales_rep_field):
            clauses.append(f"{alias}.{sales_rep_field} = ANY(%s)")
            params.append(filters["salesperson_ids"])

        return " AND ".join(clauses), params

    def _resolve_domain_and_group(self, domain: str, group_by: str) -> tuple[str, str]:
        domain = (domain or "finance").lower()
        if domain not in self.DRILL_CATALOG:
            domain = "finance"
        cfg = self.DRILL_CATALOG[domain]
        group_by = (group_by or cfg["default_group"]).lower()
        if group_by not in cfg["groups"]:
            group_by = cfg["default_group"]
        return domain, group_by

    def _resolve_metric(self, domain: str, metric: str) -> str:
        cfg = self.DRILL_CATALOG.get(domain, self.DRILL_CATALOG["finance"])
        metric = (metric or cfg["default_metric"]).lower()
        if metric not in cfg["metrics"]:
            metric = cfg["default_metric"]
        return metric

    def _real_margin_availability(self, filters: dict) -> dict:
        status = {
            "available": False,
            "reason": "missing_schema",
            "product_lines": 0,
            "costed_lines": 0,
            "coverage_pct": 0.0,
        }
        if not (self._has_table("account_move") and self._has_table("account_move_line")):
            return status
        if not (self._has_column("account_move_line", "total_cost") and self._has_column("account_move_line", "price_subtotal")):
            return status

        where_sql, params = self._build_scope_clause(
            alias="move",
            table_name="account_move",
            filters=filters,
            include_sales_rep=True,
        )
        params += [filters["start_date"], filters["end_date"]]
        self.env.cr.execute(
            f"""
            SELECT
                COUNT(*) AS product_lines,
                COUNT(*) FILTER (WHERE line.total_cost IS NOT NULL) AS costed_lines
            FROM account_move_line line
            JOIN account_move move ON move.id = line.move_id
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
              AND (line.display_type = 'product' OR line.display_type IS NULL)
            """,
            params,
        )
        row = self._dictfetchone()
        product_lines = int(row.get("product_lines") or 0)
        costed_lines = int(row.get("costed_lines") or 0)
        coverage = (float(costed_lines) / float(product_lines) * 100.0) if product_lines else 0.0
        status.update(
            {
                "product_lines": product_lines,
                "costed_lines": costed_lines,
                "coverage_pct": coverage,
            }
        )
        if product_lines > 0 and costed_lines == product_lines:
            status["available"] = True
            status["reason"] = "ok"
        elif product_lines == 0:
            status["reason"] = "no_product_lines"
        else:
            status["reason"] = "incomplete_cost_coverage"
        return status

    def _margin_summary(self, filters: dict, margin_status: dict | None = None) -> dict:
        data = {
            "net_margin": 0.0,
            "negative_margin_lines": 0.0,
            "untaxed_revenue": 0.0,
            "margin_pct": 0.0,
            "source": "none",
            "available": False,
        }
        margin_status = margin_status or self._real_margin_availability(filters)
        if not margin_status.get("available"):
            data["source"] = margin_status.get("reason", "unavailable")
            return data

        where_sql, params = self._build_scope_clause(
            alias="move",
            table_name="account_move",
            filters=filters,
            include_sales_rep=True,
        )
        params += [filters["start_date"], filters["end_date"]]

        self.env.cr.execute(
            f"""
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN move.move_type = 'out_refund'
                        THEN -(ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0)))
                        ELSE ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0))
                    END
                ), 0) AS net_margin,
                COUNT(*) FILTER (
                    WHERE move.move_type IN ('out_invoice', 'out_receipt')
                      AND (ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0))) < 0
                ) AS negative_margin_lines,
                COALESCE(SUM(
                    CASE
                        WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(line.price_subtotal, 0))
                        ELSE ABS(COALESCE(line.price_subtotal, 0))
                    END
                ), 0) AS untaxed_revenue
            FROM account_move_line line
            JOIN account_move move ON move.id = line.move_id
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
              AND (line.display_type = 'product' OR line.display_type IS NULL)
            """,
            params,
        )
        row = self._dictfetchone()
        data["net_margin"] = float(row.get("net_margin") or 0)
        data["negative_margin_lines"] = float(row.get("negative_margin_lines") or 0)
        data["untaxed_revenue"] = float(row.get("untaxed_revenue") or 0)
        data["margin_pct"] = (data["net_margin"] / data["untaxed_revenue"] * 100.0) if data["untaxed_revenue"] else 0.0
        data["source"] = "line_total_cost"
        data["available"] = True
        return data

    def _finance_summary(self, filters: dict, margin_status: dict | None = None) -> dict:
        if not self._has_table("account_move"):
            return {
                "gross_sales": 0,
                "net_revenue": 0,
                "collections_total": 0,
                "overdue_receivables": 0,
                "credit_note_value": 0,
                "net_margin": 0,
                "margin_pct": 0,
                "margin_source": "none",
                "margin_available": False,
            }

        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=filters, include_sales_rep=True)
        params += [filters["start_date"], filters["end_date"]]
        self.env.cr.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN move.move_type IN ('out_invoice','out_receipt') THEN ABS(COALESCE(move.amount_total_signed, 0)) ELSE 0 END), 0) AS gross_sales,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN ABS(COALESCE(move.amount_total_signed, 0)) ELSE 0 END), 0) AS credit_note_value,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(move.amount_total_signed, 0)) ELSE ABS(COALESCE(move.amount_total_signed, 0)) END), 0) AS net_revenue
            FROM account_move move
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            """,
            params,
        )
        summary = self._dictfetchone()

        collections_total = 0.0
        if self._has_table("account_payment"):
            where_pay_sql, pay_params = self._build_scope_clause(alias="payment", table_name="account_payment", filters=filters)
            pay_params += [filters["start_date"], filters["end_date"]]
            self.env.cr.execute(
                f"""
                SELECT COALESCE(SUM(
                    CASE WHEN payment.payment_type = 'inbound' THEN COALESCE(payment.amount, 0)
                         ELSE -COALESCE(payment.amount, 0)
                    END
                ), 0) AS collections_total
                FROM account_payment payment
                WHERE {where_pay_sql}
                  AND payment.state = 'posted'
                  AND payment.partner_type = 'customer'
                  AND payment.date BETWEEN %s AND %s
                """,
                pay_params,
            )
            collections_total = float((self._dictfetchone() or {}).get("collections_total") or 0)

        overdue_receivables = 0.0
        self.env.cr.execute(
            f"""
            SELECT COALESCE(SUM(GREATEST(COALESCE(move.amount_residual_signed, 0), 0)), 0) AS overdue_receivables
            FROM account_move move
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt')
              AND COALESCE(move.amount_residual_signed, 0) > 0
              AND move.invoice_date_due < %s
            """,
            params[:-2] + [filters["end_date"]],
        )
        overdue_receivables = float((self._dictfetchone() or {}).get("overdue_receivables") or 0)

        result = {
            "gross_sales": float(summary.get("gross_sales") or 0),
            "credit_note_value": float(summary.get("credit_note_value") or 0),
            "net_revenue": float(summary.get("net_revenue") or 0),
            "collections_total": collections_total,
            "overdue_receivables": overdue_receivables,
        }
        result["return_rate"] = (result["credit_note_value"] / result["gross_sales"] * 100.0) if result["gross_sales"] else 0.0
        margin = self._margin_summary(filters, margin_status=margin_status)
        result["margin_available"] = bool(margin.get("available"))
        if margin.get("available"):
            result["net_margin"] = margin["net_margin"]
            result["margin_pct"] = margin["margin_pct"]
        result["margin_source"] = margin["source"]
        return result

    def _sales_summary(self, filters: dict, margin_status: dict | None = None) -> dict:
        if not self._has_table("account_move"):
            return {
                "invoice_count": 0,
                "average_basket": 0,
                "net_revenue": 0,
                "net_margin": 0,
                "margin_pct": 0,
                "negative_margin_invoices": 0,
                "margin_available": False,
            }

        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=filters, include_sales_rep=True)
        params += [filters["start_date"], filters["end_date"]]
        self.env.cr.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count,
                COALESCE(AVG(ABS(move.amount_total_signed)) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')), 0) AS average_basket,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(move.amount_total_signed, 0)) ELSE ABS(COALESCE(move.amount_total_signed, 0)) END), 0) AS net_revenue
            FROM account_move move
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            """,
            params,
        )
        row = self._dictfetchone()
        margin = self._margin_summary(filters, margin_status=margin_status)
        output = {
            "invoice_count": float(row.get("invoice_count") or 0),
            "average_basket": float(row.get("average_basket") or 0),
            "net_revenue": float(row.get("net_revenue") or 0),
            "margin_available": bool(margin.get("available")),
            "negative_margin_invoices": margin["negative_margin_lines"] if margin.get("available") else 0.0,
        }
        if margin.get("available"):
            output["net_margin"] = margin["net_margin"]
            output["margin_pct"] = margin["margin_pct"]
        return output

    def _inventory_summary(self, filters: dict) -> dict:
        if not (self._has_table("stock_quant") and self._has_table("stock_valuation_layer")):
            return {"selected_scope_value": 0, "selected_on_hand_qty": 0, "zero_value_count": 0, "qty_gap_count": 0}

        quant_where, quant_params = self._build_scope_clause(alias="quant", table_name="stock_quant", filters=filters)
        svl_where, svl_params = self._build_scope_clause(alias="svl", table_name="stock_valuation_layer", filters=filters)

        self.env.cr.execute(
            f"""
            WITH quant_agg AS (
                SELECT
                    quant.product_id,
                    quant.company_id,
                    SUM(COALESCE(quant.quantity, 0)) AS on_hand_qty
                FROM stock_quant quant
                JOIN stock_location location ON location.id = quant.location_id
                WHERE {quant_where}
                  AND location.usage = 'internal'
                GROUP BY quant.product_id, quant.company_id
            ),
            svl_agg AS (
                SELECT
                    svl.product_id,
                    svl.company_id,
                    SUM(COALESCE(svl.quantity, 0)) AS svl_qty,
                    SUM(COALESCE(svl.value, 0)) AS svl_value
                FROM stock_valuation_layer svl
                WHERE {svl_where}
                GROUP BY svl.product_id, svl.company_id
            ),
            joined AS (
                SELECT
                    COALESCE(q.product_id, s.product_id) AS product_id,
                    COALESCE(q.company_id, s.company_id) AS company_id,
                    COALESCE(q.on_hand_qty, 0) AS on_hand_qty,
                    COALESCE(s.svl_qty, 0) AS svl_qty,
                    COALESCE(s.svl_value, 0) AS svl_value,
                    CASE
                        WHEN COALESCE(s.svl_qty, 0) = 0 THEN 0
                        ELSE COALESCE(q.on_hand_qty, 0) * (COALESCE(s.svl_value, 0) / NULLIF(s.svl_qty, 0))
                    END AS allocated_value
                FROM quant_agg q
                FULL OUTER JOIN svl_agg s
                  ON s.product_id = q.product_id
                 AND s.company_id = q.company_id
            )
            SELECT
                COALESCE(SUM(allocated_value), 0) AS selected_scope_value,
                COALESCE(SUM(on_hand_qty), 0) AS selected_on_hand_qty,
                COUNT(*) FILTER (WHERE on_hand_qty > 0 AND ABS(allocated_value) < 0.01) AS zero_value_count,
                COUNT(*) FILTER (WHERE ABS(on_hand_qty - svl_qty) > 10) AS qty_gap_count
            FROM joined
            """,
            quant_params + svl_params,
        )
        row = self._dictfetchone()
        return {
            "selected_scope_value": float(row.get("selected_scope_value") or 0),
            "selected_on_hand_qty": float(row.get("selected_on_hand_qty") or 0),
            "zero_value_count": float(row.get("zero_value_count") or 0),
            "qty_gap_count": float(row.get("qty_gap_count") or 0),
        }

    def _pipeline_summary(self, filters: dict) -> dict:
        if not self._has_table("crm_lead"):
            return {"open_opportunities": 0, "open_pipeline": 0, "weighted_pipeline": 0, "stalled_count": 0}

        where_sql, params = self._build_scope_clause(alias="lead", table_name="crm_lead", filters=filters, include_sales_rep=True)
        params += [filters["start_date"], filters["end_date"]]
        self.env.cr.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100) AS open_opportunities,
                COALESCE(SUM(
                    CASE WHEN lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100
                    THEN COALESCE(lead.expected_revenue, 0) ELSE 0 END
                ), 0) AS open_pipeline,
                COALESCE(SUM(
                    CASE WHEN lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100
                    THEN COALESCE(lead.expected_revenue, 0) * COALESCE(lead.probability, 0) / 100.0 ELSE 0 END
                ), 0) AS weighted_pipeline,
                COUNT(*) FILTER (
                    WHERE lead.type = 'opportunity'
                      AND lead.active IS TRUE
                      AND COALESCE(lead.probability, 0) < 100
                      AND COALESCE(lead.write_date::date, lead.create_date::date) <= CURRENT_DATE - INTERVAL '14 days'
                ) AS stalled_count
            FROM crm_lead lead
            WHERE {where_sql}
            """,
            params[:-2],
        )
        row = self._dictfetchone()
        return {
            "open_opportunities": float(row.get("open_opportunities") or 0),
            "open_pipeline": float(row.get("open_pipeline") or 0),
            "weighted_pipeline": float(row.get("weighted_pipeline") or 0),
            "stalled_count": float(row.get("stalled_count") or 0),
        }

    def _build_alerts(self, finance: dict, sales: dict, inventory: dict, pipeline: dict, fx_watch: dict) -> list:
        alerts = []

        overdue_ratio = 0.0
        if finance.get("net_revenue"):
            overdue_ratio = finance["overdue_receivables"] / max(finance["net_revenue"], 1)
        if overdue_ratio >= 0.35:
            alerts.append({"severity": "high", "label": "Receivables pressure", "detail": f"Overdue AR is {overdue_ratio:.0%} of current net revenue."})

        if sales.get("negative_margin_invoices", 0) > 0:
            alerts.append({"severity": "high", "label": "Negative margin sales", "detail": f"{int(sales['negative_margin_invoices'])} sales lines are below margin guardrails."})

        if inventory.get("zero_value_count", 0) > 0:
            alerts.append({"severity": "medium", "label": "Zero value stock", "detail": f"{int(inventory['zero_value_count'])} products carry stock with near-zero allocated value."})

        if inventory.get("qty_gap_count", 0) > 0:
            alerts.append({"severity": "medium", "label": "Quant vs SVL gap", "detail": f"{int(inventory['qty_gap_count'])} products show quantity divergence > 10 units."})

        if pipeline.get("stalled_count", 0) > 0:
            alerts.append({"severity": "low", "label": "Stalled pipeline", "detail": f"{int(pipeline['stalled_count'])} opportunities are idle for 14+ days."})

        stale_pairs = [c["pair"] for c in fx_watch.get("cards", []) if c.get("is_stale")]
        if stale_pairs:
            alerts.append({"severity": "low", "label": "FX stale feed", "detail": f"Using last good rate for: {', '.join(stale_pairs)}."})

        if not alerts:
            alerts.append({"severity": "low", "label": "No critical red flags", "detail": "Core thresholds are currently inside expected range."})

        return alerts

    def _export_drill_catalog(self, margin_available: bool):
        domains = []
        for key, cfg in self.DRILL_CATALOG.items():
            metrics_map = dict(cfg["metrics"])
            if key in {"finance", "sales"} and not margin_available:
                metrics_map.pop("net_margin", None)
                metrics_map.pop("margin_pct", None)
            domains.append(
                {
                    "key": key,
                    "label": cfg["label"],
                    "description": cfg["description"],
                    "groups": [{"key": group_key, "label": group_label} for group_key, group_label in cfg["groups"].items()],
                    "metrics": [{"key": metric_key, "label": metric_label} for metric_key, metric_label in metrics_map.items()],
                    "default_group": cfg["default_group"],
                    "default_metric": cfg["default_metric"] if cfg["default_metric"] in metrics_map else "net_revenue",
                }
            )
        return domains

    def _get_filter_options(self, scope: dict):
        if self.env.user.has_group("tradeline_executive_pocket_dashboard.group_exec_admin"):
            company_domain = []
        else:
            company_domain = [("id", "in", self.env.user.company_ids.ids)]
        companies = self.env["res.company"].sudo().search(company_domain, order="name")
        company_options = [{"id": company.id, "name": company.name} for company in companies]

        branch_options = []
        if "res.branch" in self.env:
            branch_domain = []
            if scope.get("company_ids"):
                branch_domain.append(("company_id", "in", scope["company_ids"]))
            branches = self.env["res.branch"].sudo().search(branch_domain, order="name")
            branch_options = [{"id": branch.id, "name": branch.name} for branch in branches]

        salesperson_options = []
        if "sales_rep" in self.env:
            reps = self.env["sales_rep"].sudo().search([], order="name")
            salesperson_options = [{"id": rep.id, "name": rep.name} for rep in reps]

        return {
            "companies": company_options,
            "branches": branch_options,
            "salespersons": salesperson_options,
        }

    def _data_coverage(self, scope: dict):
        coverage = {"finance": 0, "sales": 0, "inventory": 0, "pipeline": 0}
        if self._has_table("account_move"):
            where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
            params += [scope["start_date"], scope["end_date"]]
            self.env.cr.execute(
                f"""
                SELECT COUNT(*) AS count_rows
                FROM account_move move
                WHERE {where_sql}
                  AND move.state = 'posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                """,
                params,
            )
            count_moves = int((self._dictfetchone() or {}).get("count_rows") or 0)
            coverage["finance"] = count_moves
            coverage["sales"] = count_moves

        if self._has_table("stock_quant"):
            quant_where, quant_params = self._build_scope_clause(alias="quant", table_name="stock_quant", filters=scope)
            self.env.cr.execute(
                f"""
                SELECT COUNT(*) AS count_rows
                FROM stock_quant quant
                JOIN stock_location location ON location.id = quant.location_id
                WHERE {quant_where}
                  AND location.usage = 'internal'
                """,
                quant_params,
            )
            coverage["inventory"] = int((self._dictfetchone() or {}).get("count_rows") or 0)

        if self._has_table("crm_lead"):
            lead_where, lead_params = self._build_scope_clause(alias="lead", table_name="crm_lead", filters=scope, include_sales_rep=True)
            self.env.cr.execute(
                f"""
                SELECT COUNT(*) AS count_rows
                FROM crm_lead lead
                WHERE {lead_where}
                  AND lead.type = 'opportunity'
                  AND lead.active IS TRUE
                """,
                lead_params,
            )
            coverage["pipeline"] = int((self._dictfetchone() or {}).get("count_rows") or 0)

        return coverage

    def _daily_sales_snapshot(self, scope: dict) -> dict:
        window_end = scope["end_date"]
        window_start = max(scope["start_date"], window_end - timedelta(days=6))
        days = [window_start + timedelta(days=idx) for idx in range((window_end - window_start).days + 1)]
        rows_map = {}

        if self._has_table("account_move"):
            where_sql, params = self._build_scope_clause(
                alias="move",
                table_name="account_move",
                filters=scope,
                include_sales_rep=True,
            )
            params += [window_start, window_end]
            self.env.cr.execute(
                f"""
                SELECT
                    move.invoice_date::date AS day,
                    COUNT(*) AS invoice_count,
                    COALESCE(SUM(COALESCE(move.amount_untaxed_signed, 0)), 0) AS net_revenue
                FROM account_move move
                WHERE {where_sql}
                  AND move.state = 'posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY day
                ORDER BY day
                """,
                params,
            )
            for rec in self._dictfetchall():
                day = rec.get("day")
                if day:
                    rows_map[fields.Date.to_date(day)] = {
                        "invoice_count": int(rec.get("invoice_count") or 0),
                        "net_revenue": float(rec.get("net_revenue") or 0.0),
                    }

        rows = []
        total_revenue = 0.0
        total_invoices = 0
        for day in days:
            data = rows_map.get(day, {})
            invoice_count = int(data.get("invoice_count") or 0)
            net_revenue = float(data.get("net_revenue") or 0.0)
            avg_basket = (net_revenue / invoice_count) if invoice_count else 0.0
            total_revenue += net_revenue
            total_invoices += invoice_count
            rows.append(
                {
                    "date": str(day),
                    "invoice_count": invoice_count,
                    "net_revenue": net_revenue,
                    "average_basket": avg_basket,
                }
            )

        today_value = rows[-1]["net_revenue"] if rows else 0.0
        yesterday_value = rows[-2]["net_revenue"] if len(rows) > 1 else 0.0
        avg_daily_revenue = (total_revenue / len(rows)) if rows else 0.0
        avg_basket_total = (total_revenue / total_invoices) if total_invoices else 0.0

        return {
            "window_start": str(window_start),
            "window_end": str(window_end),
            "rows": rows,
            "stats": {
                "total_net_revenue": total_revenue,
                "total_invoices": total_invoices,
                "avg_daily_revenue": avg_daily_revenue,
                "avg_basket": avg_basket_total,
                "today_revenue": today_value,
                "yesterday_revenue": yesterday_value,
                "day_over_day_pct": self._percent_change(today_value, yesterday_value),
            },
        }

    @api.model
    def get_dashboard_bundle(self, filters=None, lens="overview", drill_path=None, limit=10):
        self._ensure_exec_admin()
        scope = self._resolve_filter_scope(filters)
        margin_status = self._real_margin_availability(scope)
        finance = self._finance_summary(scope, margin_status=margin_status)
        sales = self._sales_summary(scope, margin_status=margin_status)
        inventory = self._inventory_summary(scope)
        pipeline = self._pipeline_summary(scope)
        daily_snapshot = self._daily_sales_snapshot(scope)
        top_limit = max(1, min(int(limit or 10), 100))
        top_sections = self._build_top_sections(scope, top_limit, margin_status)
        fx_watch = self.get_fx_watch()
        alerts = self._build_alerts(finance, sales, inventory, pipeline, fx_watch)
        coverage = self._data_coverage(scope)
        filter_options = self._get_filter_options(scope)

        cards = [
            {"key": "net_revenue", "label": "Net Revenue", "value": finance["net_revenue"], "unit": "EGP", "tone": "neutral"},
            {"key": "collections_total", "label": "Collections", "value": finance["collections_total"], "unit": "EGP", "tone": "neutral"},
            {"key": "overdue_receivables", "label": "Overdue AR", "value": finance["overdue_receivables"], "unit": "EGP", "tone": "warning"},
            {"key": "open_pipeline", "label": "Open Pipeline", "value": pipeline["open_pipeline"], "unit": "EGP", "tone": "neutral"},
            {"key": "inventory_value", "label": "Inventory Value", "value": inventory["selected_scope_value"], "unit": "EGP", "tone": "neutral"},
            {"key": "on_hand_qty", "label": "On Hand Qty", "value": inventory["selected_on_hand_qty"], "unit": "", "tone": "neutral"},
            {"key": "invoice_count", "label": "Invoices", "value": sales["invoice_count"], "unit": "", "tone": "neutral"},
        ]
        if margin_status.get("available"):
            cards.insert(1, {"key": "net_margin", "label": "Net Margin", "value": finance.get("net_margin", 0), "unit": "EGP", "tone": "neutral"})

        default_domain = "finance"
        if drill_path and len(drill_path) > 1:
            default_domain = drill_path[1]
        default_domain, default_group = self._resolve_domain_and_group(default_domain, "")
        default_metric = self._resolve_metric(default_domain, "")
        drilldown = self.get_drilldown(
            default_domain,
            metric=default_metric,
            group_by=default_group,
            filters=filters,
            limit=25,
            offset=0,
        )

        return {
            "meta": {
                "generated_at": fields.Datetime.to_datetime(fields.Datetime.now()).isoformat(),
                "lens": lens or "overview",
                "scope": {
                    "start_date": str(scope["start_date"]),
                    "end_date": str(scope["end_date"]),
                    "company_ids": scope["company_ids"],
                    "branch_ids": scope["branch_ids"],
                    "salesperson_ids": scope["salesperson_ids"],
                },
                "margin_status": margin_status,
            },
            "cards": cards,
            "alerts": alerts,
            "coverage": coverage,
            "filter_options": filter_options,
            "drill_catalog": self._export_drill_catalog(bool(margin_status.get("available"))),
            "sections": {
                "finance": finance,
                "sales": sales,
                "inventory": inventory,
                "pipeline": pipeline,
                "daily_snapshot": daily_snapshot,
            },
            "fx_watch": fx_watch,
            "drill_path": drill_path or ["overview", default_domain, default_group, "details"],
            "drilldown": drilldown,
            "top_sections": top_sections,
        }

    @api.model
    def get_alerts(self, filters=None):
        self._ensure_exec_admin()
        scope = self._resolve_filter_scope(filters)
        finance = self._finance_summary(scope)
        sales = self._sales_summary(scope)
        inventory = self._inventory_summary(scope)
        pipeline = self._pipeline_summary(scope)
        fx_watch = self.get_fx_watch()
        return self._build_alerts(finance, sales, inventory, pipeline, fx_watch)

    @api.model
    def get_drilldown(self, domain="finance", metric="value", group_by="branch", filters=None, limit=25, offset=0):
        self._ensure_exec_admin()
        scope = self._resolve_filter_scope(filters)
        limit = max(1, min(int(limit or 25), 200))
        offset = max(0, int(offset or 0))
        domain, group_by = self._resolve_domain_and_group(domain, group_by)
        metric = self._resolve_metric(domain, metric)
        margin_status = self._real_margin_availability(scope)
        if not margin_status.get("available") and metric in {"net_margin", "margin_pct"}:
            metric = "net_revenue"

        if domain == "finance":
            result = self._finance_drilldown(scope, group_by, metric, limit, offset, margin_status)
        elif domain == "sales":
            result = self._sales_drilldown(scope, group_by, metric, limit, offset, margin_status)
        elif domain == "inventory":
            result = self._inventory_drilldown(scope, group_by, metric, limit, offset)
        elif domain == "pipeline":
            result = self._pipeline_drilldown(scope, group_by, metric, limit, offset)
        else:
            result = {
                "domain": domain,
                "group_by": group_by,
                "metric": metric,
                "rows": [],
                "columns": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
            }

        for row in result.get("rows", []):
            row["dimension"] = self._clean_dimension_label(row.get("dimension"))
        result.setdefault("total_count", len(result.get("rows", [])))
        result.setdefault("limit", limit)
        result.setdefault("offset", offset)
        return result

    def _finance_drilldown(self, scope, group_by, metric, limit, offset, margin_status):
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        params += [scope["start_date"], scope["end_date"], limit, offset]
        include_company_split = len(scope.get("company_ids") or []) > 1 and group_by != "company"
        company_join_sql = "LEFT JOIN res_company company ON company.id = move.company_id"
        has_branch = self._has_table("res_branch")
        if has_branch:
            dim_sql = "COALESCE(branch.name, 'Unassigned Branch')"
            joins = "LEFT JOIN res_branch branch ON branch.id = move.branch_id"
        else:
            dim_sql = "CONCAT('Branch #', COALESCE(move.branch_id, 0)::text)"
            joins = ""
        if group_by == "company":
            dim_sql = "COALESCE(company.name, 'Unknown Company')"
            joins = company_join_sql
        elif group_by == "customer":
            dim_sql = "COALESCE(partner.name, 'Unknown Customer')"
            joins = "LEFT JOIN res_partner partner ON partner.id = move.partner_id"
        elif group_by == "payment_state":
            dim_sql = "COALESCE(move.payment_state, 'unknown')"
            joins = ""
        if include_company_split and "res_company company" not in joins:
            joins = f"{joins}\n{company_join_sql}" if joins else company_join_sql
        company_select = ",\n                COALESCE(company.name, 'Unknown Company') AS company" if include_company_split else ""
        company_group_by = ", company" if include_company_split else ""

        order_metric = metric if metric in {"invoice_count", "net_revenue", "credit_note_value"} else "net_revenue"
        self.env.cr.execute(
            f"""
            SELECT
                {dim_sql} AS dimension{company_select},
                COUNT(*) AS invoice_count,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(move.amount_total_signed, 0)) ELSE ABS(COALESCE(move.amount_total_signed, 0)) END), 0) AS net_revenue,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN ABS(COALESCE(move.amount_total_signed, 0)) ELSE 0 END), 0) AS credit_note_value
            FROM account_move move
            {joins}
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension{company_group_by}
            ORDER BY {order_metric} DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        count_params = list(params[:-2])
        self.env.cr.execute(
            f"""
            SELECT COUNT(*) AS total_count
            FROM (
                SELECT {dim_sql} AS dimension{company_select}
                FROM account_move move
                {joins}
                WHERE {where_sql}
                  AND move.state = 'posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension{company_group_by}
            ) grouped
            """,
            count_params,
        )
        total_count = int((self._dictfetchone() or {}).get("total_count") or 0)
        columns = ["dimension", "invoice_count", "net_revenue", "credit_note_value"]
        if include_company_split:
            columns.insert(0, "company")

        if margin_status.get("available"):
            margin_params = list(params[:-2])  # remove limit/offset
            self.env.cr.execute(
                f"""
                SELECT
                    {dim_sql} AS dimension{company_select},
                    COALESCE(SUM(
                        CASE
                            WHEN move.move_type = 'out_refund'
                            THEN -(ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0)))
                            ELSE ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0))
                        END
                    ), 0) AS net_margin,
                    COALESCE(SUM(
                        CASE
                            WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(line.price_subtotal, 0))
                            ELSE ABS(COALESCE(line.price_subtotal, 0))
                        END
                    ), 0) AS margin_basis
                FROM account_move move
                JOIN account_move_line line
                  ON line.move_id = move.id
                 AND (line.display_type = 'product' OR line.display_type IS NULL)
                 AND line.total_cost IS NOT NULL
                {joins}
                WHERE {where_sql}
                  AND move.state = 'posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                  AND line.total_cost IS NOT NULL
                GROUP BY dimension{company_group_by}
                """,
                margin_params,
            )
            margin_map = {}
            for margin_row in self._dictfetchall():
                key = margin_row.get("dimension")
                if include_company_split:
                    key = (margin_row.get("company"), margin_row.get("dimension"))
                margin_map[key] = margin_row
            for row in rows:
                margin_key = row.get("dimension")
                if include_company_split:
                    margin_key = (row.get("company"), row.get("dimension"))
                margin_row = margin_map.get(margin_key) or {}
                net_margin = float(margin_row.get("net_margin") or 0.0)
                margin_basis = float(margin_row.get("margin_basis") or 0.0)
                row["net_margin"] = net_margin
                row["margin_pct"] = (net_margin / margin_basis * 100.0) if margin_basis else 0.0
            columns = ["dimension", "invoice_count", "net_revenue", "net_margin", "margin_pct", "credit_note_value"]
            if include_company_split:
                columns.insert(0, "company")
            if metric in {"net_margin", "margin_pct"}:
                rows = sorted(rows, key=lambda r: float(r.get(metric) or 0.0), reverse=True)

        return {
            "domain": "finance",
            "group_by": group_by,
            "metric": metric,
            "columns": columns,
            "rows": rows,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    def _sales_drilldown(self, scope, group_by, metric, limit, offset, margin_status):
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        params += [scope["start_date"], scope["end_date"], limit, offset]
        include_company_split = len(scope.get("company_ids") or []) > 1 and group_by != "company"
        company_join_sql = "LEFT JOIN res_company company ON company.id = move.company_id"
        has_branch = self._has_table("res_branch")
        if has_branch:
            dim_sql = "COALESCE(branch.name, 'Unassigned Branch')"
            joins = "LEFT JOIN res_branch branch ON branch.id = move.branch_id"
        else:
            dim_sql = "CONCAT('Branch #', COALESCE(move.branch_id, 0)::text)"
            joins = ""
        if group_by == "company":
            dim_sql = "COALESCE(company.name, 'Unknown Company')"
            joins = company_join_sql
        elif group_by == "salesperson":
            if self._has_table("sales_rep"):
                dim_sql = "COALESCE(sales_rep.name, 'Unknown Sales Rep')"
                joins = "LEFT JOIN sales_rep ON sales_rep.id = move.sales_rep_id"
            else:
                dim_sql = "CONCAT('Sales Rep #', COALESCE(move.sales_rep_id, 0)::text)"
                joins = ""
        elif group_by == "customer":
            dim_sql = "COALESCE(partner.name, 'Unknown Customer')"
            joins = "LEFT JOIN res_partner partner ON partner.id = move.partner_id"
        elif group_by == "category":
            dim_sql = "COALESCE(category.complete_name, 'Unclassified')"
            joins = """
                JOIN account_move_line line
                  ON line.move_id = move.id
                 AND (line.display_type = 'product' OR line.display_type IS NULL)
                LEFT JOIN product_product product ON product.id = line.product_id
                LEFT JOIN product_template template ON template.id = product.product_tmpl_id
                LEFT JOIN product_category category ON category.id = template.categ_id
            """
        elif group_by == "product":
            dim_sql = "COALESCE(line.product_id, 0)"
            joins = """
                JOIN account_move_line line
                  ON line.move_id = move.id
                 AND (line.display_type = 'product' OR line.display_type IS NULL)
                LEFT JOIN product_product product ON product.id = line.product_id
                LEFT JOIN product_template template ON template.id = product.product_tmpl_id
            """
        if include_company_split and "res_company company" not in joins:
            joins = f"{joins}\n{company_join_sql}" if joins else company_join_sql
        company_select = ",\n                COALESCE(company.name, 'Unknown Company') AS company" if include_company_split else ""
        company_group_by = ", company" if include_company_split else ""
        order_metric = metric if metric in {"invoice_count", "average_basket", "net_revenue"} else "net_revenue"
        self.env.cr.execute(
            f"""
            SELECT
                {dim_sql} AS dimension{company_select},
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count,
                COALESCE(AVG(ABS(move.amount_total_signed)) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')), 0) AS average_basket,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(move.amount_total_signed, 0)) ELSE ABS(COALESCE(move.amount_total_signed, 0)) END), 0) AS net_revenue
            FROM account_move move
            {joins}
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension{company_group_by}
            ORDER BY {order_metric} DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        count_params = list(params[:-2])
        self.env.cr.execute(
            f"""
            SELECT COUNT(*) AS total_count
            FROM (
                SELECT {dim_sql} AS dimension{company_select}
                FROM account_move move
                {joins}
                WHERE {where_sql}
                  AND move.state = 'posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension{company_group_by}
            ) grouped
            """,
            count_params,
        )
        total_count = int((self._dictfetchone() or {}).get("total_count") or 0)
        columns = ["dimension", "invoice_count", "average_basket", "net_revenue"]
        if include_company_split:
            columns.insert(0, "company")

        if margin_status.get("available"):
            margin_joins = joins
            if group_by not in {"category", "product"}:
                margin_joins = (
                    "JOIN account_move_line line ON line.move_id = move.id AND (line.display_type = 'product' OR line.display_type IS NULL) AND line.total_cost IS NOT NULL\n"
                    + joins
                )
            margin_params = list(params[:-2])  # remove limit/offset
            self.env.cr.execute(
                f"""
                SELECT
                    {dim_sql} AS dimension{company_select},
                    COALESCE(SUM(
                        CASE
                            WHEN move.move_type = 'out_refund'
                            THEN -(ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0)))
                            ELSE ABS(COALESCE(line.price_subtotal, 0)) - ABS(COALESCE(line.total_cost, 0))
                        END
                    ), 0) AS net_margin,
                    COALESCE(SUM(
                        CASE
                            WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(line.price_subtotal, 0))
                            ELSE ABS(COALESCE(line.price_subtotal, 0))
                        END
                    ), 0) AS margin_basis
                FROM account_move move
                {margin_joins}
                WHERE {where_sql}
                  AND move.state = 'posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension{company_group_by}
                """,
                margin_params,
            )
            margin_map = {}
            for margin_row in self._dictfetchall():
                key = margin_row.get("dimension")
                if include_company_split:
                    key = (margin_row.get("company"), margin_row.get("dimension"))
                margin_map[key] = margin_row
            for row in rows:
                margin_key = row.get("dimension")
                if include_company_split:
                    margin_key = (row.get("company"), row.get("dimension"))
                margin_row = margin_map.get(margin_key) or {}
                net_margin = float(margin_row.get("net_margin") or 0.0)
                margin_basis = float(margin_row.get("margin_basis") or 0.0)
                row["net_margin"] = net_margin
                row["margin_pct"] = (net_margin / margin_basis * 100.0) if margin_basis else 0.0
            columns = ["dimension", "invoice_count", "average_basket", "net_revenue", "net_margin", "margin_pct"]
            if include_company_split:
                columns.insert(0, "company")
            if metric in {"net_margin", "margin_pct"}:
                rows = sorted(rows, key=lambda r: float(r.get(metric) or 0.0), reverse=True)

        if group_by == "product":
            product_ids = []
            for row in rows:
                try:
                    row["_dimension_product_id"] = int(row.get("dimension") or 0)
                except Exception:
                    row["_dimension_product_id"] = 0
                if row["_dimension_product_id"] > 0:
                    product_ids.append(row["_dimension_product_id"])

            name_map = {}
            if product_ids:
                products = (
                    self.env["product.product"]
                    .sudo()
                    .with_context(lang=self.env.user.lang or "en_US")
                    .browse(sorted(set(product_ids)))
                    .exists()
                )
                for product in products:
                    name_map[product.id] = product.display_name or product.name or f"Product #{product.id}"

            for row in rows:
                product_id = row.pop("_dimension_product_id", 0)
                if product_id > 0:
                    row["dimension"] = name_map.get(product_id, f"Product #{product_id}")
                else:
                    row["dimension"] = "Unspecified Product"

        return {
            "domain": "sales",
            "group_by": group_by,
            "metric": metric,
            "columns": columns,
            "rows": rows,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    def _inventory_drilldown(self, scope, group_by, metric, limit, offset):
        quant_where, quant_params = self._build_scope_clause(alias="quant", table_name="stock_quant", filters=scope)
        svl_where, svl_params = self._build_scope_clause(alias="svl", table_name="stock_valuation_layer", filters=scope)
        params = quant_params + svl_params + [limit, offset]
        include_company_split = len(scope.get("company_ids") or []) > 1 and group_by != "company"
        company_join_sql = "LEFT JOIN res_company company ON company.id = inv.company_id"

        if group_by == "company":
            dim_sql = "COALESCE(company.name, 'Unknown Company')"
            dim_join = "LEFT JOIN res_company company ON company.id = inv.company_id"
        elif group_by == "product":
            dim_sql = "COALESCE(inv.product_id, 0)"
            dim_join = ""
        else:
            dim_sql = "COALESCE(category.complete_name, 'Unclassified')"
            dim_join = """
                LEFT JOIN product_product product ON product.id = inv.product_id
                LEFT JOIN product_template template ON template.id = product.product_tmpl_id
                LEFT JOIN product_category category ON category.id = template.categ_id
            """
        if include_company_split and "res_company company" not in dim_join:
            dim_join = f"{dim_join}\n{company_join_sql}" if dim_join else company_join_sql
        company_select = ",\n                COALESCE(company.name, 'Unknown Company') AS company" if include_company_split else ""
        company_group_by = ", company" if include_company_split else ""

        order_metric = metric if metric in {"allocated_value", "on_hand_qty", "unit_cost"} else "allocated_value"
        self.env.cr.execute(
            f"""
            WITH quant_agg AS (
                SELECT quant.product_id, quant.company_id, SUM(COALESCE(quant.quantity, 0)) AS on_hand_qty
                FROM stock_quant quant
                JOIN stock_location location ON location.id = quant.location_id
                WHERE {quant_where}
                  AND location.usage = 'internal'
                GROUP BY quant.product_id, quant.company_id
            ),
            svl_agg AS (
                SELECT svl.product_id, svl.company_id, SUM(COALESCE(svl.quantity, 0)) AS svl_qty, SUM(COALESCE(svl.value, 0)) AS svl_value
                FROM stock_valuation_layer svl
                WHERE {svl_where}
                GROUP BY svl.product_id, svl.company_id
            ),
            inv AS (
                SELECT
                    COALESCE(q.product_id, s.product_id) AS product_id,
                    COALESCE(q.company_id, s.company_id) AS company_id,
                    COALESCE(q.on_hand_qty, 0) AS on_hand_qty,
                    COALESCE(s.svl_qty, 0) AS svl_qty,
                    COALESCE(s.svl_value, 0) AS svl_value,
                    CASE WHEN COALESCE(s.svl_qty, 0) = 0 THEN 0
                         ELSE COALESCE(q.on_hand_qty, 0) * (COALESCE(s.svl_value, 0) / NULLIF(s.svl_qty, 0))
                    END AS allocated_value
                FROM quant_agg q
                FULL OUTER JOIN svl_agg s
                  ON s.product_id = q.product_id
                 AND s.company_id = q.company_id
            )
            SELECT
                {dim_sql} AS dimension{company_select},
                COALESCE(SUM(inv.on_hand_qty), 0) AS on_hand_qty,
                COALESCE(SUM(inv.allocated_value), 0) AS allocated_value,
                CASE
                    WHEN COALESCE(SUM(inv.on_hand_qty), 0) = 0 THEN 0
                    ELSE COALESCE(SUM(inv.allocated_value), 0) / NULLIF(COALESCE(SUM(inv.on_hand_qty), 0), 0)
                END AS unit_cost
            FROM inv
            {dim_join}
            GROUP BY dimension{company_group_by}
            ORDER BY {order_metric} DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        count_params = quant_params + svl_params
        self.env.cr.execute(
            f"""
            WITH quant_agg AS (
                SELECT quant.product_id, quant.company_id, SUM(COALESCE(quant.quantity, 0)) AS on_hand_qty
                FROM stock_quant quant
                JOIN stock_location location ON location.id = quant.location_id
                WHERE {quant_where}
                  AND location.usage = 'internal'
                GROUP BY quant.product_id, quant.company_id
            ),
            svl_agg AS (
                SELECT svl.product_id, svl.company_id, SUM(COALESCE(svl.quantity, 0)) AS svl_qty, SUM(COALESCE(svl.value, 0)) AS svl_value
                FROM stock_valuation_layer svl
                WHERE {svl_where}
                GROUP BY svl.product_id, svl.company_id
            ),
            inv AS (
                SELECT
                    COALESCE(q.product_id, s.product_id) AS product_id,
                    COALESCE(q.company_id, s.company_id) AS company_id,
                    COALESCE(q.on_hand_qty, 0) AS on_hand_qty,
                    COALESCE(s.svl_qty, 0) AS svl_qty,
                    COALESCE(s.svl_value, 0) AS svl_value,
                    CASE WHEN COALESCE(s.svl_qty, 0) = 0 THEN 0
                         ELSE COALESCE(q.on_hand_qty, 0) * (COALESCE(s.svl_value, 0) / NULLIF(s.svl_qty, 0))
                    END AS allocated_value
                FROM quant_agg q
                FULL OUTER JOIN svl_agg s
                  ON s.product_id = q.product_id
                 AND s.company_id = q.company_id
            )
            SELECT COUNT(*) AS total_count
            FROM (
                SELECT {dim_sql} AS dimension{company_select}
                FROM inv
                {dim_join}
                GROUP BY dimension{company_group_by}
            ) grouped
            """,
            count_params,
        )
        total_count = int((self._dictfetchone() or {}).get("total_count") or 0)

        if group_by == "product":
            product_ids = []
            for row in rows:
                try:
                    row["_dimension_product_id"] = int(row.get("dimension") or 0)
                except Exception:
                    row["_dimension_product_id"] = 0
                if row["_dimension_product_id"] > 0:
                    product_ids.append(row["_dimension_product_id"])

            name_map = {}
            if product_ids:
                products = (
                    self.env["product.product"]
                    .sudo()
                    .with_context(lang=self.env.user.lang or "en_US")
                    .browse(sorted(set(product_ids)))
                    .exists()
                )
                for product in products:
                    name_map[product.id] = product.display_name or product.name or f"Product #{product.id}"

            for row in rows:
                product_id = row.pop("_dimension_product_id", 0)
                if product_id > 0:
                    row["dimension"] = name_map.get(product_id, f"Product #{product_id}")
                else:
                    row["dimension"] = "Unspecified Product"

        return {
            "domain": "inventory",
            "group_by": group_by,
            "metric": metric,
            "columns": (["company"] if include_company_split else []) + ["dimension", "on_hand_qty", "allocated_value", "unit_cost"],
            "rows": rows,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    def _pipeline_drilldown(self, scope, group_by, metric, limit, offset):
        where_sql, params = self._build_scope_clause(alias="lead", table_name="crm_lead", filters=scope, include_sales_rep=True)
        params += [limit, offset]
        include_company_split = len(scope.get("company_ids") or []) > 1 and group_by != "company"
        company_join_sql = "LEFT JOIN res_company company ON company.id = lead.company_id"

        dim_sql = "COALESCE(stage.name, CONCAT('Stage #', COALESCE(lead.stage_id, 0)::text))"
        joins = "LEFT JOIN crm_stage stage ON stage.id = lead.stage_id"
        if group_by == "company":
            dim_sql = "COALESCE(company.name, 'Unknown Company')"
            joins = company_join_sql
        elif group_by == "owner":
            if self._has_table("sales_rep"):
                dim_sql = "COALESCE(user_partner.name, sales_rep.name, CONCAT('Owner #', COALESCE(lead.user_id, 0)::text))"
                joins = """
                    LEFT JOIN sales_rep ON sales_rep.id = lead.sales_rep_id
                    LEFT JOIN res_users u ON u.id = lead.user_id
                    LEFT JOIN res_partner user_partner ON user_partner.id = u.partner_id
                """
            else:
                dim_sql = "COALESCE(user_partner.name, CONCAT('Owner #', COALESCE(lead.user_id, 0)::text))"
                joins = """
                    LEFT JOIN res_users u ON u.id = lead.user_id
                    LEFT JOIN res_partner user_partner ON user_partner.id = u.partner_id
                """
        elif group_by == "branch":
            if self._has_table("res_branch"):
                dim_sql = "COALESCE(branch.name, 'Unassigned Branch')"
                joins = "LEFT JOIN res_branch branch ON branch.id = lead.branch_id"
            else:
                dim_sql = "CONCAT('Branch #', COALESCE(lead.branch_id, 0)::text)"
                joins = ""
        if include_company_split and "res_company company" not in joins:
            joins = f"{joins}\n{company_join_sql}" if joins else company_join_sql
        company_select = ",\n                COALESCE(company.name, 'Unknown Company') AS company" if include_company_split else ""
        company_group_by = ", company" if include_company_split else ""

        order_metric = metric if metric in {"open_opportunities", "open_pipeline", "weighted_pipeline"} else "weighted_pipeline"
        self.env.cr.execute(
            f"""
            SELECT
                {dim_sql} AS dimension{company_select},
                COUNT(*) FILTER (WHERE lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100) AS open_opportunities,
                COALESCE(SUM(CASE WHEN lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100 THEN COALESCE(lead.expected_revenue, 0) ELSE 0 END), 0) AS open_pipeline,
                COALESCE(SUM(CASE WHEN lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100 THEN COALESCE(lead.expected_revenue, 0) * COALESCE(lead.probability, 0) / 100.0 ELSE 0 END), 0) AS weighted_pipeline
            FROM crm_lead lead
            {joins}
            WHERE {where_sql}
            GROUP BY dimension{company_group_by}
            ORDER BY {order_metric} DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        count_params = list(params[:-2])
        self.env.cr.execute(
            f"""
            SELECT COUNT(*) AS total_count
            FROM (
                SELECT {dim_sql} AS dimension{company_select}
                FROM crm_lead lead
                {joins}
                WHERE {where_sql}
                GROUP BY dimension{company_group_by}
            ) grouped
            """,
            count_params,
        )
        total_count = int((self._dictfetchone() or {}).get("total_count") or 0)
        return {
            "domain": "pipeline",
            "group_by": group_by,
            "metric": metric,
            "columns": (["company"] if include_company_split else []) + ["dimension", "open_opportunities", "open_pipeline", "weighted_pipeline"],
            "rows": rows,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    @api.model
    def get_top_sections(self, filters=None, limit=10):
        self._ensure_exec_admin()
        scope = self._resolve_filter_scope(filters)
        limit = max(1, min(int(limit or 10), 100))
        margin_status = self._real_margin_availability(scope)
        return self._build_top_sections(scope, limit, margin_status)

    def _single_day_sales(self, scope, target_date):
        if not self._has_table("account_move"):
            return 0.0
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope)
        params.append(target_date)
        self.env.cr.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0))
                ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS total
            FROM account_move move WHERE {where_sql} AND move.state='posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date = %s
        """, params)
        return float((self._dictfetchone() or {}).get("total") or 0.0)

    def _build_top_sections(self, scope, limit, margin_status=None):
        margin_status = margin_status or self._real_margin_availability(scope)
        report_date = scope.get("report_date") or scope["end_date"]
        
        # Scopes for daily report metrics
        today_scope = dict(scope, start_date=report_date, end_date=report_date)
        yesterday_date = report_date - timedelta(days=1)
        yesterday_scope = dict(scope, start_date=yesterday_date, end_date=yesterday_date)
        
        mtd_start = report_date.replace(day=1)
        mtd_scope = dict(scope, start_date=mtd_start, end_date=report_date)
        
        snapshot_start = max(scope["start_date"], report_date - timedelta(days=6))
        snapshot_scope = dict(scope, start_date=snapshot_start, end_date=report_date)
        
        # Queries for report date
        today_sales_val = self._single_day_sales(today_scope, report_date)
        yesterday_sales_val = self._single_day_sales(yesterday_scope, yesterday_date)
        acc = self._acc_sales_mtd(mtd_scope)
        attachment = self._attachment_rate(today_scope)
        
        company_ids = scope.get("company_ids") or []
        company_names = [c.name for c in self.env["res.company"].sudo().browse(company_ids) if c.name]
        return {
            "sales_by_branch": self._top_sales_by_branch(scope, limit, margin_status),
            "sales_by_salesperson": self._top_sales_by_salesperson(scope, limit, margin_status),
            "sales_by_category": self._top_sales_by_category(scope, limit, margin_status),
            "sales_by_customer": self._top_sales_by_customer(scope, limit, margin_status),
            "inventory_by_category": self._top_inventory_by_category(scope, limit),
            "sales_over_month": self._sales_over_month(scope),
            "attachment_rate": attachment["rate"],
            "total_invoices": attachment["total_invoices"],
            "acc_sales": acc["acc_sales"],
            "acc_sales_prev_day": acc["acc_sales_prev_day"],
            "today_sales": today_sales_val,
            "yesterday_sales": yesterday_sales_val,
            "margin_available": bool(margin_status.get("available")),
            "company_names": company_names,
            "limit": limit,
        }

    def _top_sales_by_branch(self, scope, limit, margin_status=None):
        if not self._has_table("account_move"):
            return []
        has_branch = self._has_table("res_branch")
        dim_sql = "COALESCE(branch.name, 'Unassigned')" if has_branch else "'All'"
        join_sql = "LEFT JOIN res_branch branch ON branch.id = move.branch_id" if has_branch else ""
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        base_params = list(params) + [scope["start_date"], scope["end_date"]]
        self.env.cr.execute(f"""
            SELECT
                {dim_sql} AS dimension,
                COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0)) ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS net_revenue,
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count
            FROM account_move move {join_sql}
            WHERE {where_sql}
              AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension ORDER BY net_revenue DESC LIMIT %s
        """, base_params + [limit])
        rows = self._dictfetchall()
        if margin_status and margin_status.get("available") and has_branch:
            self.env.cr.execute(f"""
                SELECT
                    {dim_sql} AS dimension,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund'
                        THEN -(ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)))
                        ELSE ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)) END),0) AS net_margin,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(line.price_subtotal,0)) ELSE ABS(COALESCE(line.price_subtotal,0)) END),0) AS margin_basis
                FROM account_move move
                JOIN account_move_line line ON line.move_id=move.id
                  AND (line.display_type='product' OR line.display_type IS NULL) AND line.total_cost IS NOT NULL
                {join_sql}
                WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension
            """, base_params)
            mmap = {r["dimension"]: r for r in self._dictfetchall()}
            for row in rows:
                m = mmap.get(row.get("dimension")) or {}
                row["net_margin"] = float(m.get("net_margin") or 0)
                mb = float(m.get("margin_basis") or 0)
                row["margin_pct"] = (row["net_margin"] / mb * 100) if mb else 0.0
        for row in rows:
            row["net_revenue"] = float(row.get("net_revenue") or 0)
            row["invoice_count"] = int(row.get("invoice_count") or 0)
            row["dimension"] = self._clean_dimension_label(row.get("dimension"))
        return rows

    def _top_sales_by_salesperson(self, scope, limit, margin_status=None):
        if not self._has_table("account_move"):
            return []
        has_sr = self._has_table("sales_rep")
        if has_sr:
            dim_sql = "COALESCE(sr.name, 'Unassigned')"
            join_sql = "LEFT JOIN sales_rep sr ON sr.id = move.sales_rep_id"
        else:
            dim_sql = "COALESCE(up.name, 'Unassigned')"
            join_sql = "LEFT JOIN res_users u ON u.id = move.invoice_user_id LEFT JOIN res_partner up ON up.id = u.partner_id"
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        base_params = list(params) + [scope["start_date"], scope["end_date"]]
        self.env.cr.execute(f"""
            SELECT
                {dim_sql} AS dimension,
                COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0)) ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS net_revenue,
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count
            FROM account_move move {join_sql}
            WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension ORDER BY net_revenue DESC LIMIT %s
        """, base_params + [limit])
        rows = self._dictfetchall()
        if margin_status and margin_status.get("available"):
            self.env.cr.execute(f"""
                SELECT
                    {dim_sql} AS dimension,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund'
                        THEN -(ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)))
                        ELSE ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)) END),0) AS net_margin,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(line.price_subtotal,0)) ELSE ABS(COALESCE(line.price_subtotal,0)) END),0) AS margin_basis
                FROM account_move move
                JOIN account_move_line line ON line.move_id=move.id
                  AND (line.display_type='product' OR line.display_type IS NULL) AND line.total_cost IS NOT NULL
                {join_sql}
                WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension
            """, base_params)
            mmap = {r["dimension"]: r for r in self._dictfetchall()}
            for row in rows:
                m = mmap.get(row.get("dimension")) or {}
                row["net_margin"] = float(m.get("net_margin") or 0)
                mb = float(m.get("margin_basis") or 0)
                row["margin_pct"] = (row["net_margin"] / mb * 100) if mb else 0.0
        for row in rows:
            row["net_revenue"] = float(row.get("net_revenue") or 0)
            row["invoice_count"] = int(row.get("invoice_count") or 0)
            row["dimension"] = self._clean_dimension_label(row.get("dimension"))
        return rows

    def _top_sales_by_category(self, scope, limit, margin_status=None):
        if not self._has_table("account_move"):
            return []
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        base_params = list(params) + [scope["start_date"], scope["end_date"]]
        cat_joins = """
            JOIN account_move_line line ON line.move_id=move.id AND (line.display_type='product' OR line.display_type IS NULL)
            LEFT JOIN product_product product ON product.id=line.product_id
            LEFT JOIN product_template template ON template.id=product.product_tmpl_id
            LEFT JOIN product_category category ON category.id=template.categ_id
        """
        self.env.cr.execute(f"""
            SELECT
                COALESCE(category.complete_name,'Unclassified') AS dimension,
                COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(line.price_subtotal,0)) ELSE ABS(COALESCE(line.price_subtotal,0)) END),0) AS net_revenue,
                COUNT(DISTINCT move.id) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count
            FROM account_move move {cat_joins}
            WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension ORDER BY net_revenue DESC LIMIT %s
        """, base_params + [limit])
        rows = self._dictfetchall()
        if margin_status and margin_status.get("available"):
            marg_joins = """
                JOIN account_move_line line ON line.move_id=move.id
                  AND (line.display_type='product' OR line.display_type IS NULL) AND line.total_cost IS NOT NULL
                LEFT JOIN product_product product ON product.id=line.product_id
                LEFT JOIN product_template template ON template.id=product.product_tmpl_id
                LEFT JOIN product_category category ON category.id=template.categ_id
            """
            self.env.cr.execute(f"""
                SELECT
                    COALESCE(category.complete_name,'Unclassified') AS dimension,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund'
                        THEN -(ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)))
                        ELSE ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)) END),0) AS net_margin,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(line.price_subtotal,0)) ELSE ABS(COALESCE(line.price_subtotal,0)) END),0) AS margin_basis
                FROM account_move move {marg_joins}
                WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension
            """, base_params)
            mmap = {r["dimension"]: r for r in self._dictfetchall()}
            for row in rows:
                m = mmap.get(row.get("dimension")) or {}
                row["net_margin"] = float(m.get("net_margin") or 0)
                mb = float(m.get("margin_basis") or 0)
                row["margin_pct"] = (row["net_margin"] / mb * 100) if mb else 0.0
        for row in rows:
            row["net_revenue"] = float(row.get("net_revenue") or 0)
            row["invoice_count"] = int(row.get("invoice_count") or 0)
        return rows

    def _top_sales_by_customer(self, scope, limit, margin_status=None):
        if not self._has_table("account_move"):
            return []
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        base_params = list(params) + [scope["start_date"], scope["end_date"]]
        self.env.cr.execute(f"""
            SELECT
                COALESCE(partner.name,'Unknown') AS dimension,
                COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0)) ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS net_revenue,
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count
            FROM account_move move
            LEFT JOIN res_partner partner ON partner.id=move.partner_id
            WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension ORDER BY net_revenue DESC LIMIT %s
        """, base_params + [limit])
        rows = self._dictfetchall()
        if margin_status and margin_status.get("available"):
            self.env.cr.execute(f"""
                SELECT
                    COALESCE(partner.name,'Unknown') AS dimension,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund'
                        THEN -(ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)))
                        ELSE ABS(COALESCE(line.price_subtotal,0))-ABS(COALESCE(line.total_cost,0)) END),0) AS net_margin,
                    COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(line.price_subtotal,0)) ELSE ABS(COALESCE(line.price_subtotal,0)) END),0) AS margin_basis
                FROM account_move move
                JOIN account_move_line line ON line.move_id=move.id
                  AND (line.display_type='product' OR line.display_type IS NULL) AND line.total_cost IS NOT NULL
                LEFT JOIN res_partner partner ON partner.id=move.partner_id
                WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY dimension
            """, base_params)
            mmap = {r["dimension"]: r for r in self._dictfetchall()}
            for row in rows:
                m = mmap.get(row.get("dimension")) or {}
                row["net_margin"] = float(m.get("net_margin") or 0)
                mb = float(m.get("margin_basis") or 0)
                row["margin_pct"] = (row["net_margin"] / mb * 100) if mb else 0.0
        for row in rows:
            row["net_revenue"] = float(row.get("net_revenue") or 0)
            row["invoice_count"] = int(row.get("invoice_count") or 0)
            row["dimension"] = self._clean_dimension_label(row.get("dimension"))
        return rows

    def _top_inventory_by_category(self, scope, limit):
        if not (self._has_table("stock_quant") and self._has_table("stock_valuation_layer")):
            return []
        quant_where, quant_params = self._build_scope_clause(alias="quant", table_name="stock_quant", filters=scope)
        svl_where, svl_params = self._build_scope_clause(alias="svl", table_name="stock_valuation_layer", filters=scope)
        self.env.cr.execute(f"""
            WITH quant_agg AS (
                SELECT quant.product_id, quant.company_id, SUM(COALESCE(quant.quantity,0)) AS on_hand_qty
                FROM stock_quant quant
                JOIN stock_location location ON location.id=quant.location_id
                WHERE {quant_where} AND location.usage='internal'
                GROUP BY quant.product_id, quant.company_id
            ),
            svl_agg AS (
                SELECT svl.product_id, svl.company_id,
                    SUM(COALESCE(svl.quantity,0)) AS svl_qty, SUM(COALESCE(svl.value,0)) AS svl_value
                FROM stock_valuation_layer svl WHERE {svl_where}
                GROUP BY svl.product_id, svl.company_id
            ),
            inv AS (
                SELECT
                    COALESCE(q.product_id,s.product_id) AS product_id,
                    COALESCE(q.company_id,s.company_id) AS company_id,
                    COALESCE(q.on_hand_qty,0) AS on_hand_qty,
                    CASE WHEN COALESCE(s.svl_qty,0)=0 THEN 0
                         ELSE COALESCE(q.on_hand_qty,0)*(COALESCE(s.svl_value,0)/NULLIF(s.svl_qty,0))
                    END AS allocated_value
                FROM quant_agg q FULL OUTER JOIN svl_agg s ON s.product_id=q.product_id AND s.company_id=q.company_id
            )
            SELECT
                COALESCE(category.complete_name,'Unclassified') AS dimension,
                COALESCE(SUM(inv.on_hand_qty),0) AS on_hand_qty,
                COALESCE(SUM(inv.allocated_value),0) AS allocated_value
            FROM inv
            LEFT JOIN product_product product ON product.id=inv.product_id
            LEFT JOIN product_template template ON template.id=product.product_tmpl_id
            LEFT JOIN product_category category ON category.id=template.categ_id
            GROUP BY dimension ORDER BY allocated_value DESC LIMIT %s
        """, quant_params + svl_params + [limit])
        rows = self._dictfetchall()
        for row in rows:
            row["on_hand_qty"] = float(row.get("on_hand_qty") or 0)
            row["allocated_value"] = float(row.get("allocated_value") or 0)
        return rows

    def _sales_over_month(self, scope):
        if not self._has_table("account_move"):
            return []
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        params += [scope["start_date"], scope["end_date"]]
        self.env.cr.execute(f"""
            SELECT
                move.invoice_date::date AS day,
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count,
                COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0))
                    ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS net_revenue
            FROM account_move move
            WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY day ORDER BY day
        """, params)
        return [
            {"date": str(r["day"]), "net_revenue": float(r.get("net_revenue") or 0), "invoice_count": int(r.get("invoice_count") or 0)}
            for r in self._dictfetchall()
        ]

    def _attachment_rate(self, scope):
        if not self._has_table("account_move"):
            return {"rate": 0.0, "total_invoices": 0, "multi_item_invoices": 0}
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope)
        params += [scope["start_date"], scope["end_date"]]
        self.env.cr.execute(f"""
            WITH invoice_products AS (
                SELECT move.id,
                    COUNT(line.id) FILTER (WHERE line.display_type='product' OR line.display_type IS NULL) AS product_line_count
                FROM account_move move
                LEFT JOIN account_move_line line ON line.move_id=move.id
                WHERE {where_sql} AND move.state='posted' AND move.move_type IN ('out_invoice','out_receipt')
                  AND move.invoice_date BETWEEN %s AND %s
                GROUP BY move.id
            )
            SELECT
                COUNT(*) AS total_invoices,
                COUNT(*) FILTER (WHERE product_line_count >= 2) AS multi_item_invoices
            FROM invoice_products
        """, params)
        row = self._dictfetchone() or {}
        total = int(row.get("total_invoices") or 0)
        multi = int(row.get("multi_item_invoices") or 0)
        return {"rate": (multi / total * 100) if total else 0.0, "total_invoices": total, "multi_item_invoices": multi}

    def _acc_sales_mtd(self, scope):
        if not self._has_table("account_move"):
            return {"acc_sales": 0.0, "acc_sales_prev_day": 0.0}
        end_date = scope["end_date"]
        mtd_start = end_date.replace(day=1)
        prev_end = end_date - timedelta(days=1)
        mtd_scope = dict(scope, start_date=mtd_start)
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=mtd_scope)
        params += [mtd_start, end_date]
        self.env.cr.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0))
                ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS total
            FROM account_move move WHERE {where_sql} AND move.state='posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
        """, params)
        acc_sales = float((self._dictfetchone() or {}).get("total") or 0)
        acc_prev = 0.0
        if prev_end >= mtd_start:
            prev_scope = dict(scope, start_date=mtd_start, end_date=prev_end)
            w2, p2 = self._build_scope_clause(alias="move", table_name="account_move", filters=prev_scope)
            p2 += [mtd_start, prev_end]
            self.env.cr.execute(f"""
                SELECT COALESCE(SUM(CASE WHEN move.move_type='out_refund' THEN -ABS(COALESCE(move.amount_total_signed,0))
                    ELSE ABS(COALESCE(move.amount_total_signed,0)) END),0) AS total
                FROM account_move move WHERE {w2} AND move.state='posted'
                  AND move.move_type IN ('out_invoice','out_receipt','out_refund')
                  AND move.invoice_date BETWEEN %s AND %s
            """, p2)
            acc_prev = float((self._dictfetchone() or {}).get("total") or 0)
        return {"acc_sales": acc_sales, "acc_sales_prev_day": acc_prev}

    @api.model
    def get_fx_watch(self):
        self._ensure_exec_admin()
        pairs = list(self.FX_TARGETS.keys())
        records = []
        model = self.env["tradeline.executive.fx.rate"].sudo()
        legacy_aliases = {
            "USD/EGP": "EGP/USD",
            "EUR/EGP": "EGP/EUR",
            "GBP/EGP": "EGP/GBP",
        }
        for pair in pairs:
            rec = model.search([("pair", "=", pair)], order="fetched_at desc,id desc", limit=1)
            if not rec:
                legacy = model.search([("pair", "=", legacy_aliases.get(pair, ""))], order="fetched_at desc,id desc", limit=1)
                if legacy:
                    fallback_rate = (1.0 / legacy.rate) if legacy.rate else 0.0
                    rec = model.new(
                        {
                            "pair": pair,
                            "rate": fallback_rate,
                            "change_pct": -(legacy.change_pct or 0.0),
                            "status": legacy.status,
                            "is_stale": legacy.is_stale,
                            "message": f"Derived from legacy pair {legacy.pair}",
                            "fetched_at": legacy.fetched_at,
                            "source_symbol": legacy.source_symbol,
                            "source_name": legacy.source_name,
                        }
                    )
            if rec:
                records.append(rec)

        cards = []
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        for rec in records:
            age_minutes = 999999
            if rec.fetched_at:
                age_seconds = (now - rec.fetched_at).total_seconds()
                age_minutes = int(max(0, age_seconds) // 60)
            is_stale = bool(rec.is_stale or rec.status != "ok" or age_minutes > 6)

            history = model.search([("pair", "=", rec.pair)], order="fetched_at desc", limit=30)
            if not history and rec.pair in legacy_aliases:
                legacy_history = model.search([("pair", "=", legacy_aliases[rec.pair])], order="fetched_at desc", limit=30)
                sparkline = list(reversed([(1.0 / h.rate) if h.rate else 0.0 for h in legacy_history]))
            else:
                sparkline = list(reversed([h.rate for h in history]))
            period_changes = self._compute_period_changes(model, rec.pair, rec.rate or 0.0, now)
            period_changes = self._fill_missing_period_changes(period_changes, rec.pair, rec.rate or 0.0, now)
            one_day_change = period_changes.get("1D")
            if one_day_change is None:
                one_day_change = rec.change_pct or 0.0

            cards.append(
                {
                    "pair": rec.pair,
                    "rate": rec.rate,
                    "change_pct": one_day_change,
                    "period_changes": period_changes,
                    "status": rec.status,
                    "is_stale": is_stale,
                    "last_update": rec.fetched_at.isoformat() if rec.fetched_at else None,
                    "message": rec.message or "",
                    "age_minutes": age_minutes,
                    "sparkline": sparkline,
                    "display_label": f"1 {rec.pair.split('/')[0]} = {rec.pair.split('/')[1]}",
                }
            )

        return {
            "pairs": pairs,
            "cards": cards,
            "generated_at": now.isoformat(),
        }

    def _fetch_yahoo_quotes(self, symbols: list[str]) -> dict:
        quote_url = "https://query1.finance.yahoo.com/v7/finance/quote"
        headers = self._yahoo_headers()
        params = {"symbols": ",".join(symbols)}

        last_error = None
        for attempt in range(1, 4):
            try:
                response = requests.get(quote_url, params=params, headers=headers, timeout=8)
                response.raise_for_status()
                payload = response.json()
                items = ((payload or {}).get("quoteResponse") or {}).get("result") or []
                quotes = {item.get("symbol"): item for item in items if item.get("symbol")}
                if quotes:
                    return quotes
            except Exception as exc:
                last_error = exc
                sleep_for = min(4, attempt)
                time.sleep(sleep_for)

        # Fallback path: chart endpoint works in environments where quote endpoint returns 401.
        chart_url = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        fallback_quotes = {}
        fallback_errors = []
        for symbol in symbols:
            try:
                response = requests.get(
                    chart_url.format(symbol=symbol),
                    params={"interval": "1d", "range": "5d"},
                    headers=headers,
                    timeout=8,
                )
                response.raise_for_status()
                payload = response.json()
                result = ((payload or {}).get("chart") or {}).get("result") or []
                meta = (result[0] or {}).get("meta") if result else {}
                if not meta:
                    raise RuntimeError("missing chart.meta")
                fallback_quotes[symbol] = {
                    "symbol": symbol,
                    "regularMarketPrice": meta.get("regularMarketPrice"),
                    "regularMarketPreviousClose": meta.get("previousClose"),
                    "regularMarketTime": meta.get("regularMarketTime"),
                }
            except Exception as exc:
                fallback_errors.append(f"{symbol}: {exc}")

        if fallback_quotes:
            return fallback_quotes

        raise RuntimeError(
            f"Yahoo finance request failed after retries. quote_error={last_error}; chart_errors={'; '.join(fallback_errors)}"
        )

    def _yahoo_headers(self):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        }

    def _fetch_yahoo_chart_points(self, symbol: str, range_window: str = "1y", interval: str = "1d") -> list[tuple]:
        chart_url = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        response = requests.get(
            chart_url.format(symbol=symbol),
            params={"interval": interval, "range": range_window},
            headers=self._yahoo_headers(),
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        result = ((payload or {}).get("chart") or {}).get("result") or []
        if not result:
            return []
        block = result[0] or {}
        timestamps = block.get("timestamp") or []
        quote = (((block.get("indicators") or {}).get("quote") or [{}])[0] or {})
        closes = quote.get("close") or []
        points = []
        for idx, ts in enumerate(timestamps):
            if idx >= len(closes):
                continue
            close = closes[idx]
            if close in (None, False):
                continue
            try:
                rate = float(close)
            except Exception:
                continue
            if rate <= 0:
                continue
            dt = fields.Datetime.to_datetime(datetime.utcfromtimestamp(int(ts)))
            points.append((dt, rate))
        return points

    def _period_changes_from_points(self, points: list[tuple], current_rate: float, now_dt, invert: bool = False) -> dict:
        normalized = []
        for point_dt, raw_rate in points or []:
            if not raw_rate:
                continue
            rate = (1.0 / raw_rate) if invert else raw_rate
            if rate and rate > 0:
                normalized.append((point_dt, float(rate)))

        output = {}
        for label, days in self.FX_PERIOD_DAYS.items():
            cutoff = now_dt - timedelta(days=days)
            baseline = None
            for point_dt, rate in normalized:
                if point_dt <= cutoff:
                    baseline = rate
                else:
                    break
            if baseline is None and normalized:
                baseline = normalized[0][1]
            output[label] = self._percent_change(current_rate, baseline) if baseline else None
        return output

    def _fill_missing_period_changes(self, period_changes: dict, pair: str, current_rate: float, now_dt) -> dict:
        output = dict(period_changes or {})
        if not any(value is None for value in output.values()):
            return output
        cfg = self.FX_TARGETS.get(pair) or {}
        symbol = cfg.get("source_symbol")
        invert = bool(cfg.get("invert"))
        if not symbol or not current_rate:
            return output
        try:
            points = self._fetch_yahoo_chart_points(symbol=symbol, range_window="1y", interval="1d")
            fallback = self._period_changes_from_points(points, current_rate=current_rate, now_dt=now_dt, invert=invert)
            for label, value in fallback.items():
                if output.get(label) is None:
                    output[label] = value
        except Exception:
            _logger.exception("FX period fallback failed for %s", pair)
        return output

    def _backfill_fx_period_anchors(self, model, pair: str, current_rate: float, fetched_at):
        if not current_rate:
            return 0
        missing_labels = []
        for label, days in self.FX_PERIOD_DAYS.items():
            cutoff = fetched_at - timedelta(days=days)
            baseline = model.search(
                [("pair", "=", pair), ("status", "=", "ok"), ("fetched_at", "<=", cutoff)],
                order="fetched_at desc,id desc",
                limit=1,
            )
            if not baseline:
                missing_labels.append(label)
        if not missing_labels:
            return 0

        cfg = self.FX_TARGETS.get(pair) or {}
        symbol = cfg.get("source_symbol")
        invert = bool(cfg.get("invert"))
        if not symbol:
            return 0

        try:
            points = self._fetch_yahoo_chart_points(symbol=symbol, range_window="1y", interval="1d")
        except Exception:
            _logger.exception("FX backfill failed while fetching chart for %s", pair)
            return 0

        normalized = []
        for point_dt, raw_rate in points:
            rate = (1.0 / raw_rate) if invert else raw_rate
            if rate and rate > 0:
                normalized.append((point_dt, rate))
        if not normalized:
            return 0

        created = 0
        for label in missing_labels:
            days = self.FX_PERIOD_DAYS[label]
            cutoff = fetched_at - timedelta(days=days)
            anchor = None
            for point_dt, rate in normalized:
                if point_dt <= cutoff:
                    anchor = (point_dt, rate)
                else:
                    break
            if not anchor:
                anchor = normalized[0]
            anchor_dt, anchor_rate = anchor
            if anchor_rate <= 0:
                continue
            existing = model.search(
                [
                    ("pair", "=", pair),
                    ("status", "=", "ok"),
                    ("fetched_at", ">=", anchor_dt - timedelta(hours=12)),
                    ("fetched_at", "<=", anchor_dt + timedelta(hours=12)),
                ],
                limit=1,
            )
            if existing:
                continue
            model.create(
                {
                    "pair": pair,
                    "rate": anchor_rate,
                    "change_pct": self._percent_change(current_rate, anchor_rate) or 0.0,
                    "source_name": "Yahoo Finance",
                    "source_symbol": symbol,
                    "source_timestamp": anchor_dt,
                    "fetched_at": anchor_dt,
                    "is_stale": False,
                    "status": "ok",
                    "message": f"historical backfill anchor ({label})",
                    "inverted_from_symbol": symbol if invert else False,
                }
            )
            created += 1
        return created

    def _to_datetime_from_epoch(self, value):
        if not value:
            return False
        try:
            return fields.Datetime.to_datetime(datetime.utcfromtimestamp(int(value)))
        except Exception:
            return False

    def _percent_change(self, current_rate: float, baseline_rate: float):
        current_rate = float(current_rate or 0.0)
        baseline_rate = float(baseline_rate or 0.0)
        if baseline_rate <= 0:
            return None
        return ((current_rate - baseline_rate) / baseline_rate) * 100.0

    def _compute_period_changes(self, model, pair: str, current_rate: float, now_dt):
        output = {}
        for label, days in self.FX_PERIOD_DAYS.items():
            cutoff = now_dt - timedelta(days=days)
            baseline = model.search(
                [("pair", "=", pair), ("status", "=", "ok"), ("fetched_at", "<=", cutoff)],
                order="fetched_at desc,id desc",
                limit=1,
            )
            output[label] = self._percent_change(current_rate, baseline.rate) if baseline else None
        return output

    @api.model
    def refresh_fx_rates(self):
        self._ensure_exec_admin()
        return self.sudo()._refresh_fx_rates_impl(manual=True)

    @api.model
    def refresh_fx_rates_cron(self):
        return self.sudo()._refresh_fx_rates_impl(manual=False)

    def _refresh_fx_rates_impl(self, manual=False):
        model = self.env["tradeline.executive.fx.rate"].sudo()
        symbols = sorted({cfg["source_symbol"] for cfg in self.FX_TARGETS.values()})
        created = 0
        errors = []

        quotes = {}
        try:
            quotes = self._fetch_yahoo_quotes(symbols)
        except Exception as exc:
            errors.append(str(exc))
            _logger.exception("FX refresh failed while calling Yahoo")

        fetched_at = fields.Datetime.to_datetime(fields.Datetime.now())
        for pair, cfg in self.FX_TARGETS.items():
            source_symbol = cfg["source_symbol"]
            invert = cfg["invert"]
            quote = quotes.get(source_symbol) if quotes else None

            try:
                if quote:
                    market_price = float(quote.get("regularMarketPrice") or 0.0)
                    prev_close = float(quote.get("regularMarketPreviousClose") or 0.0)
                    if market_price <= 0:
                        raise ValueError(f"Invalid market price for {source_symbol}: {market_price}")

                    if invert:
                        rate = 1.0 / market_price
                        if prev_close > 0:
                            inv_prev = 1.0 / prev_close
                            change_pct = ((rate - inv_prev) / inv_prev) * 100.0
                        else:
                            change_pct = 0.0
                    else:
                        rate = market_price
                        if prev_close > 0:
                            change_pct = ((rate - prev_close) / prev_close) * 100.0
                        else:
                            change_pct = 0.0

                    model.create(
                        {
                            "pair": pair,
                            "rate": rate,
                            "change_pct": change_pct,
                            "source_name": "Yahoo Finance",
                            "source_symbol": source_symbol,
                            "source_timestamp": self._to_datetime_from_epoch(quote.get("regularMarketTime")),
                            "fetched_at": fetched_at,
                            "is_stale": False,
                            "status": "ok",
                            "message": "live",
                            "inverted_from_symbol": source_symbol if invert else False,
                        }
                    )
                    created += 1
                    created += self._backfill_fx_period_anchors(model, pair, rate, fetched_at)
                else:
                    last_good = model.search(
                        [("pair", "=", pair), ("status", "=", "ok")],
                        order="fetched_at desc,id desc",
                        limit=1,
                    )
                    fallback_rate = last_good.rate if last_good else 0.0
                    model.create(
                        {
                            "pair": pair,
                            "rate": fallback_rate,
                            "change_pct": last_good.change_pct if last_good else 0.0,
                            "source_name": "Yahoo Finance",
                            "source_symbol": source_symbol,
                            "source_timestamp": False,
                            "fetched_at": fetched_at,
                            "is_stale": True,
                            "status": "error",
                            "message": "missing quote payload; using last good value",
                            "inverted_from_symbol": source_symbol if invert else False,
                        }
                    )
                    created += 1
                    created += self._backfill_fx_period_anchors(model, pair, fallback_rate, fetched_at)
                    errors.append(f"No quote for {source_symbol}")
            except Exception as exc:
                last_good = model.search(
                    [("pair", "=", pair), ("status", "=", "ok")],
                    order="fetched_at desc,id desc",
                    limit=1,
                )
                model.create(
                    {
                        "pair": pair,
                        "rate": last_good.rate if last_good else 0.0,
                        "change_pct": last_good.change_pct if last_good else 0.0,
                        "source_name": "Yahoo Finance",
                        "source_symbol": source_symbol,
                        "source_timestamp": False,
                        "fetched_at": fetched_at,
                        "is_stale": True,
                        "status": "error",
                        "message": f"exception: {exc}",
                        "inverted_from_symbol": source_symbol if invert else False,
                    }
                )
                created += 1
                created += self._backfill_fx_period_anchors(model, pair, float(last_good.rate or 0.0), fetched_at)
                errors.append(f"{pair}: {exc}")
                _logger.exception("FX refresh failed for pair %s", pair)

        retention_cutoff = fetched_at - timedelta(days=400)
        old_recs = model.search([("fetched_at", "<", retention_cutoff)])
        if old_recs:
            old_recs.unlink()

        return {
            "ok": len(errors) == 0,
            "manual": bool(manual),
            "created": created,
            "errors": errors,
            "fetched_at": fetched_at.isoformat(),
        }

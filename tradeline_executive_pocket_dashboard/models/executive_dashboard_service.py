from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ExecutiveDashboardService(models.AbstractModel):
    _name = "tradeline.executive.dashboard.service"
    _description = "Executive Dashboard Service"

    FX_TARGETS = {
        "USD/EGP": {"source_symbol": "USDEGP=X", "invert": False},
        "EUR/EGP": {"source_symbol": "EUREGP=X", "invert": False},
        "GBP/EGP": {"source_symbol": "GBPEGP=X", "invert": False},
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

    def _resolve_filter_scope(self, filters: dict | None) -> dict:
        filters = filters or {}
        today = fields.Date.context_today(self)
        default_start = today.replace(day=1)

        start_date = self._parse_date(filters.get("start_date"), default_start)
        end_date = self._parse_date(filters.get("end_date"), today)
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

    def _finance_summary(self, filters: dict) -> dict:
        if not self._has_table("account_move"):
            return {"gross_sales": 0, "net_revenue": 0, "collections_total": 0, "overdue_receivables": 0, "credit_note_value": 0}

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
        return result

    def _sales_summary(self, filters: dict) -> dict:
        if not self._has_table("account_move"):
            return {"invoice_count": 0, "average_basket": 0, "net_revenue": 0, "negative_margin_invoices": 0}

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
        return {
            "invoice_count": float(row.get("invoice_count") or 0),
            "average_basket": float(row.get("average_basket") or 0),
            "net_revenue": float(row.get("net_revenue") or 0),
            "negative_margin_invoices": 0.0,
        }

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

    @api.model
    def get_dashboard_bundle(self, filters=None, lens="overview", drill_path=None):
        scope = self._resolve_filter_scope(filters)
        finance = self._finance_summary(scope)
        sales = self._sales_summary(scope)
        inventory = self._inventory_summary(scope)
        pipeline = self._pipeline_summary(scope)
        fx_watch = self.get_fx_watch()
        alerts = self._build_alerts(finance, sales, inventory, pipeline, fx_watch)

        cards = [
            {"key": "net_revenue", "label": "Net Revenue", "value": finance["net_revenue"], "unit": "EGP", "tone": "neutral"},
            {"key": "collections_total", "label": "Collections", "value": finance["collections_total"], "unit": "EGP", "tone": "neutral"},
            {"key": "overdue_receivables", "label": "Overdue AR", "value": finance["overdue_receivables"], "unit": "EGP", "tone": "warning"},
            {"key": "open_pipeline", "label": "Open Pipeline", "value": pipeline["open_pipeline"], "unit": "EGP", "tone": "neutral"},
            {"key": "inventory_value", "label": "Inventory Value", "value": inventory["selected_scope_value"], "unit": "EGP", "tone": "neutral"},
            {"key": "invoice_count", "label": "Invoices", "value": sales["invoice_count"], "unit": "", "tone": "neutral"},
        ]

        default_domain = drill_path[0] if drill_path else "finance"
        drilldown = self.get_drilldown(
            default_domain,
            metric="value",
            group_by="branch",
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
            },
            "cards": cards,
            "alerts": alerts,
            "sections": {
                "finance": finance,
                "sales": sales,
                "inventory": inventory,
                "pipeline": pipeline,
            },
            "fx_watch": fx_watch,
            "drill_path": drill_path or ["overview", default_domain, "branch", "details"],
            "drilldown": drilldown,
        }

    @api.model
    def get_alerts(self, filters=None):
        scope = self._resolve_filter_scope(filters)
        finance = self._finance_summary(scope)
        sales = self._sales_summary(scope)
        inventory = self._inventory_summary(scope)
        pipeline = self._pipeline_summary(scope)
        fx_watch = self.get_fx_watch()
        return self._build_alerts(finance, sales, inventory, pipeline, fx_watch)

    @api.model
    def get_drilldown(self, domain="finance", metric="value", group_by="branch", filters=None, limit=25, offset=0):
        scope = self._resolve_filter_scope(filters)
        limit = max(1, min(int(limit or 25), 200))
        offset = max(0, int(offset or 0))
        domain = (domain or "finance").lower()
        group_by = (group_by or "branch").lower()

        if domain == "finance":
            return self._finance_drilldown(scope, group_by, limit, offset)
        if domain == "sales":
            return self._sales_drilldown(scope, group_by, limit, offset)
        if domain == "inventory":
            return self._inventory_drilldown(scope, group_by, limit, offset)
        if domain == "pipeline":
            return self._pipeline_drilldown(scope, group_by, limit, offset)
        return {"domain": domain, "group_by": group_by, "rows": [], "columns": []}

    def _finance_drilldown(self, scope, group_by, limit, offset):
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        params += [scope["start_date"], scope["end_date"], limit, offset]
        has_branch = self._has_table("res_branch")
        if has_branch:
            dim_sql = "COALESCE(branch.name, 'Unassigned Branch')"
            joins = "LEFT JOIN res_branch branch ON branch.id = move.branch_id"
        else:
            dim_sql = "CONCAT('Branch #', COALESCE(move.branch_id, 0)::text)"
            joins = ""
        if group_by == "customer":
            dim_sql = "COALESCE(partner.name, 'Unknown Customer')"
            joins = "LEFT JOIN res_partner partner ON partner.id = move.partner_id"
        elif group_by == "payment_state":
            dim_sql = "COALESCE(move.payment_state, 'unknown')"
            joins = ""

        self.env.cr.execute(
            f"""
            SELECT
                {dim_sql} AS dimension,
                COUNT(*) AS invoice_count,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(move.amount_total_signed, 0)) ELSE ABS(COALESCE(move.amount_total_signed, 0)) END), 0) AS net_revenue,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN ABS(COALESCE(move.amount_total_signed, 0)) ELSE 0 END), 0) AS credit_note_value
            FROM account_move move
            {joins}
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension
            ORDER BY net_revenue DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        return {"domain": "finance", "group_by": group_by, "columns": ["dimension", "invoice_count", "net_revenue", "credit_note_value"], "rows": rows}

    def _sales_drilldown(self, scope, group_by, limit, offset):
        where_sql, params = self._build_scope_clause(alias="move", table_name="account_move", filters=scope, include_sales_rep=True)
        params += [scope["start_date"], scope["end_date"], limit, offset]
        has_branch = self._has_table("res_branch")
        if has_branch:
            dim_sql = "COALESCE(branch.name, 'Unassigned Branch')"
            joins = "LEFT JOIN res_branch branch ON branch.id = move.branch_id"
        else:
            dim_sql = "CONCAT('Branch #', COALESCE(move.branch_id, 0)::text)"
            joins = ""
        if group_by == "salesperson":
            if self._has_table("sales_rep"):
                dim_sql = "COALESCE(sales_rep.name, 'Unknown Sales Rep')"
                joins = "LEFT JOIN sales_rep ON sales_rep.id = move.sales_rep_id"
            else:
                dim_sql = "CONCAT('Sales Rep #', COALESCE(move.sales_rep_id, 0)::text)"
                joins = ""
        elif group_by == "customer":
            dim_sql = "COALESCE(partner.name, 'Unknown Customer')"
            joins = "LEFT JOIN res_partner partner ON partner.id = move.partner_id"

        self.env.cr.execute(
            f"""
            SELECT
                {dim_sql} AS dimension,
                COUNT(*) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')) AS invoice_count,
                COALESCE(AVG(ABS(move.amount_total_signed)) FILTER (WHERE move.move_type IN ('out_invoice','out_receipt')), 0) AS average_basket,
                COALESCE(SUM(CASE WHEN move.move_type = 'out_refund' THEN -ABS(COALESCE(move.amount_total_signed, 0)) ELSE ABS(COALESCE(move.amount_total_signed, 0)) END), 0) AS net_revenue
            FROM account_move move
            {joins}
            WHERE {where_sql}
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice','out_receipt','out_refund')
              AND move.invoice_date BETWEEN %s AND %s
            GROUP BY dimension
            ORDER BY net_revenue DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        return {"domain": "sales", "group_by": group_by, "columns": ["dimension", "invoice_count", "average_basket", "net_revenue"], "rows": rows}

    def _inventory_drilldown(self, scope, group_by, limit, offset):
        quant_where, quant_params = self._build_scope_clause(alias="quant", table_name="stock_quant", filters=scope)
        svl_where, svl_params = self._build_scope_clause(alias="svl", table_name="stock_valuation_layer", filters=scope)
        params = quant_params + svl_params + [limit, offset]

        if group_by == "company":
            dim_sql = "COALESCE(company.name, 'Unknown Company')"
            dim_join = "LEFT JOIN res_company company ON company.id = inv.company_id"
        elif group_by == "product":
            dim_sql = "COALESCE(template.name::text, product.default_code, CONCAT('Product #', inv.product_id::text))"
            dim_join = """
                LEFT JOIN product_product product ON product.id = inv.product_id
                LEFT JOIN product_template template ON template.id = product.product_tmpl_id
            """
        else:
            dim_sql = "COALESCE(category.complete_name, 'Unclassified')"
            dim_join = """
                LEFT JOIN product_product product ON product.id = inv.product_id
                LEFT JOIN product_template template ON template.id = product.product_tmpl_id
                LEFT JOIN product_category category ON category.id = template.categ_id
            """

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
                {dim_sql} AS dimension,
                COALESCE(SUM(inv.on_hand_qty), 0) AS on_hand_qty,
                COALESCE(SUM(inv.allocated_value), 0) AS allocated_value
            FROM inv
            {dim_join}
            GROUP BY dimension
            ORDER BY allocated_value DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        return {"domain": "inventory", "group_by": group_by, "columns": ["dimension", "on_hand_qty", "allocated_value"], "rows": rows}

    def _pipeline_drilldown(self, scope, group_by, limit, offset):
        where_sql, params = self._build_scope_clause(alias="lead", table_name="crm_lead", filters=scope, include_sales_rep=True)
        params += [limit, offset]

        dim_sql = "COALESCE(stage.name, CONCAT('Stage #', COALESCE(lead.stage_id, 0)::text))"
        joins = "LEFT JOIN crm_stage stage ON stage.id = lead.stage_id"
        if group_by == "owner":
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

        self.env.cr.execute(
            f"""
            SELECT
                {dim_sql} AS dimension,
                COUNT(*) FILTER (WHERE lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100) AS open_opportunities,
                COALESCE(SUM(CASE WHEN lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100 THEN COALESCE(lead.expected_revenue, 0) ELSE 0 END), 0) AS open_pipeline,
                COALESCE(SUM(CASE WHEN lead.type = 'opportunity' AND lead.active IS TRUE AND COALESCE(lead.probability, 0) < 100 THEN COALESCE(lead.expected_revenue, 0) * COALESCE(lead.probability, 0) / 100.0 ELSE 0 END), 0) AS weighted_pipeline
            FROM crm_lead lead
            {joins}
            WHERE {where_sql}
            GROUP BY dimension
            ORDER BY weighted_pipeline DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = self._dictfetchall()
        return {"domain": "pipeline", "group_by": group_by, "columns": ["dimension", "open_opportunities", "open_pipeline", "weighted_pipeline"], "rows": rows}

    @api.model
    def get_fx_watch(self):
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
            cards.append(
                {
                    "pair": rec.pair,
                    "rate": rec.rate,
                    "change_pct": rec.change_pct or 0.0,
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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        }
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

    def _to_datetime_from_epoch(self, value):
        if not value:
            return False
        try:
            return fields.Datetime.to_datetime(datetime.utcfromtimestamp(int(value)))
        except Exception:
            return False

    @api.model
    def refresh_fx_rates(self):
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
                errors.append(f"{pair}: {exc}")
                _logger.exception("FX refresh failed for pair %s", pair)

        retention_cutoff = fetched_at - timedelta(days=30)
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

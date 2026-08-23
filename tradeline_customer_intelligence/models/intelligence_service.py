from __future__ import annotations

import base64
import csv
import io
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class TradelineCustomerIntelligenceService(models.AbstractModel):
    _name = "tradeline.customer.intelligence.service"
    _description = "Tradeline Customer Intelligence Service"

    SOURCE_LABELS = {
        "current": "Odoo 18 live",
        "legacy": "Odoo 12 migrated archive",
    }

    def _ensure_access(self):
        if not (
            self.env.user.has_group("tradeline_customer_intelligence.group_intelligence_viewer")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError("Tradeline Intelligence is restricted to authorized decision makers.")

    def _has_table(self, table_name):
        self.env.cr.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
        return bool(self.env.cr.fetchone()[0])

    def _date_range(self, start_date=None, end_date=None):
        today = fields.Date.context_today(self)
        start = fields.Date.to_date(start_date) if start_date else today.replace(day=1)
        end = fields.Date.to_date(end_date) if end_date else today
        if start > end:
            start, end = end, start
        return start, end

    def _company_ids(self):
        return self.env.user.company_ids.ids or [self.env.company.id]

    def _normalize_entity(self, entity, query):
        entity = dict(entity or {})
        entity_type = entity.get("type") if entity.get("type") in {"category", "product", "variant"} else "query"
        try:
            entity_id = int(entity.get("id") or 0) if entity_type != "query" else 0
        except (TypeError, ValueError):
            entity_id = 0
        name = (entity.get("name") or query or "").strip()
        if entity_type != "query":
            model_name = {
                "category": "product.category",
                "product": "product.template",
                "variant": "product.product",
            }[entity_type]
            record = self.env[model_name].sudo().browse(entity_id).exists()
            if not record:
                entity_type = "query"
                entity_id = 0
            else:
                name = record.display_name
        category_ids = []
        if entity_type == "category":
            category_ids = self.env["product.category"].sudo().search([("id", "child_of", entity_id)]).ids
            category_ids = category_ids or [entity_id]
        return {"type": entity_type, "id": entity_id, "name": name, "category_ids": category_ids}

    def _anchor_clause(self, entity, query, *, source, scoped=True):
        if scoped:
            product_id = "product_id"
            template_id = "product_tmpl_id"
            category_id = "category_id"
            product_name = "product_name"
            item_code = "item_code"
        elif source == "current":
            product_id = "line.product_id"
            template_id = "product.product_tmpl_id"
            category_id = "template.categ_id"
            product_name = "COALESCE(template.name->>'en_US', template.name->>'en', '')"
            item_code = "COALESCE(product.default_code, '')"
        else:
            product_id = "line.product_id"
            template_id = "line.product_tmpl_id"
            category_id = "line.product_category_id"
            product_name = "COALESCE(line.product_name, line.name, '')"
            item_code = "COALESCE(line.item_code, '')"

        if entity["type"] == "variant":
            return f"{product_id} = %s", [entity["id"]]
        if entity["type"] == "product":
            return f"{template_id} = %s", [entity["id"]]
        if entity["type"] == "category":
            return f"{category_id} = ANY(%s)", [entity["category_ids"]]
        pattern = f"%{query.strip()}%"
        return f"({product_name} ILIKE %s OR {item_code} ILIKE %s)", [pattern, pattern]

    def _query_current(self, query, start, end, limit, entity):
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source="current", scoped=True)
        direct_anchor_sql, direct_anchor_params = self._anchor_clause(entity, query, source="current", scoped=False)
        company_ids = self._company_ids()
        self.env.cr.execute(
            """
            WITH scope_lines AS (
                SELECT
                    move.id AS basket_id,
                    move.partner_id,
                    move.invoice_date,
                    line.product_id,
                    product.product_tmpl_id,
                    template.categ_id AS category_id,
                    COALESCE(
                        NULLIF(template.name->>'en_US', ''),
                        NULLIF(template.name->>'en', ''),
                        NULLIF(product.default_code, ''),
                        'Product ' || line.product_id::text
                    ) AS product_name,
                    COALESCE(product.default_code, '') AS item_code,
                    line.quantity,
                    line.price_subtotal AS revenue
                FROM account_move_line line
                JOIN account_move move ON move.id = line.move_id
                JOIN product_product product ON product.id = line.product_id
                JOIN product_template template ON template.id = product.product_tmpl_id
                WHERE move.state = 'posted'
                  AND move.move_type IN ('out_invoice', 'out_receipt')
                  AND move.invoice_date BETWEEN %s AND %s
                  AND move.company_id = ANY(%s)
                  AND line.product_id IS NOT NULL
                  AND line.quantity > 0
                  AND (line.display_type = 'product' OR line.display_type IS NULL)
            ),
            anchor_baskets AS (
                SELECT DISTINCT basket_id
                FROM scope_lines
                WHERE {anchor_sql}
            ),
            anchor_names AS (
                SELECT product_name, COUNT(DISTINCT basket_id) AS baskets
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND {anchor_sql}
                GROUP BY product_name
                ORDER BY baskets DESC, product_name
                LIMIT 1
            ),
            basket_companions AS (
                SELECT DISTINCT basket_id, product_id, product_name
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND NOT ({anchor_sql})
            ),
            companion_totals AS (
                SELECT product_id, product_name, COUNT(DISTINCT basket_id) AS co_baskets
                FROM basket_companions
                GROUP BY product_id, product_name
            ),
            base_totals AS (
                SELECT product_id, COUNT(DISTINCT basket_id) AS base_baskets
                FROM scope_lines
                GROUP BY product_id
            ),
            totals AS (
                SELECT
                    (SELECT COUNT(*) FROM anchor_baskets) AS anchor_baskets,
                    (SELECT COUNT(DISTINCT basket_id) FROM basket_companions) AS companion_baskets,
                    COUNT(DISTINCT basket_id) AS all_baskets,
                    COUNT(DISTINCT basket_id) FILTER (
                        WHERE partner_id IS NOT NULL
                          AND basket_id IN (SELECT basket_id FROM anchor_baskets)
                    ) AS identified_baskets,
                    COUNT(DISTINCT partner_id) FILTER (
                        WHERE partner_id IS NOT NULL
                          AND basket_id IN (SELECT basket_id FROM anchor_baskets)
                    ) AS identified_customers
                FROM scope_lines
            )
            SELECT
                companion.product_id::text AS product_key,
                companion.product_name,
                companion.co_baskets,
                base.base_baskets,
                totals.anchor_baskets,
                totals.companion_baskets,
                totals.all_baskets,
                totals.identified_baskets,
                totals.identified_customers,
                (SELECT product_name FROM anchor_names) AS anchor_name
            FROM companion_totals companion
            JOIN base_totals base ON base.product_id = companion.product_id
            CROSS JOIN totals
            ORDER BY companion.co_baskets DESC, companion.product_name
            LIMIT %s
            """.format(anchor_sql=anchor_sql),
            [
                start,
                end,
                company_ids,
                *anchor_params,
                *anchor_params,
                *anchor_params,
                limit,
            ],
        )
        columns = [column[0] for column in self.env.cr.description]
        companions = [dict(zip(columns, row)) for row in self.env.cr.fetchall()]

        self.env.cr.execute(
            """
            WITH scope_lines AS (
                SELECT
                    move.id AS basket_id,
                    move.partner_id,
                    move.invoice_date,
                    move.journal_id,
                    line.product_id,
                    product.product_tmpl_id,
                    template.categ_id AS category_id,
                    COALESCE(template.name->>'en_US', template.name->>'en', product.default_code, '') AS product_name,
                    COALESCE(product.default_code, '') AS item_code,
                    line.price_subtotal AS revenue
                FROM account_move_line line
                JOIN account_move move ON move.id = line.move_id
                JOIN product_product product ON product.id = line.product_id
                JOIN product_template template ON template.id = product.product_tmpl_id
                WHERE move.state = 'posted'
                  AND move.move_type IN ('out_invoice', 'out_receipt')
                  AND move.invoice_date BETWEEN %s AND %s
                  AND move.company_id = ANY(%s)
                  AND line.product_id IS NOT NULL
                  AND line.quantity > 0
                  AND (line.display_type = 'product' OR line.display_type IS NULL)
            ),
            anchors AS (
                SELECT DISTINCT basket_id
                FROM scope_lines
                WHERE {anchor_sql}
            )
            SELECT
                partner.id AS partner_id,
                partner.name,
                partner.email,
                partner.mobile,
                COUNT(DISTINCT lines.basket_id) AS baskets,
                SUM(lines.revenue) AS revenue,
                MAX(lines.invoice_date) AS last_purchase
            FROM scope_lines lines
            JOIN anchors ON anchors.basket_id = lines.basket_id
            JOIN res_partner partner ON partner.id = lines.partner_id
            GROUP BY partner.id, partner.name, partner.email, partner.mobile
            ORDER BY baskets DESC, revenue DESC
            LIMIT 50
            """.format(anchor_sql=anchor_sql),
            [start, end, company_ids, *anchor_params],
        )
        customer_columns = [column[0] for column in self.env.cr.description]
        customers = [dict(zip(customer_columns, row)) for row in self.env.cr.fetchall()]

        self.env.cr.execute(
            """
            WITH anchor_moves AS (
                SELECT DISTINCT move.id, move.partner_id, move.journal_id
                FROM account_move_line line
                JOIN account_move move ON move.id = line.move_id
                JOIN product_product product ON product.id = line.product_id
                JOIN product_template template ON template.id = product.product_tmpl_id
                WHERE move.state = 'posted'
                  AND move.move_type IN ('out_invoice', 'out_receipt')
                  AND move.invoice_date BETWEEN %s AND %s
                  AND move.company_id = ANY(%s)
                  AND line.quantity > 0
                  AND {anchor_sql}
            )
            SELECT
                CASE
                    WHEN LOWER(COALESCE(journal.name->>'en_US', journal.name->>'en', '')) LIKE '%%valu%%' THEN 'ValU'
                    WHEN LOWER(COALESCE(journal.name->>'en_US', journal.name->>'en', '')) LIKE '%%souh%%' THEN 'Souhoola'
                    WHEN LOWER(COALESCE(journal.name->>'en_US', journal.name->>'en', '')) SIMILAR TO '%%(cash|bank|card|visa)%%' THEN 'Cash / Card'
                    ELSE 'Other'
                END AS payment_group,
                COUNT(*) AS baskets
            FROM anchor_moves move
            LEFT JOIN account_journal journal ON journal.id = move.journal_id
            GROUP BY payment_group
            ORDER BY baskets DESC
            """.format(anchor_sql=direct_anchor_sql),
            [start, end, company_ids, *direct_anchor_params],
        )
        payments = [{"name": row[0], "baskets": row[1]} for row in self.env.cr.fetchall()]
        return companions, customers, payments

    def _query_legacy(self, query, start, end, limit, entity):
        if not self._has_table("legacy_invoice_line"):
            return [], [], []
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=True)
        direct_anchor_sql, direct_anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=False)
        company_ids = self._company_ids()
        self.env.cr.execute(
            """
            WITH scope_lines AS (
                SELECT
                    invoice.id AS basket_id,
                    invoice.partner_id,
                    invoice.invoice_date,
                    line.product_id,
                    line.product_tmpl_id,
                    line.product_category_id AS category_id,
                    COALESCE(
                        'product:' || line.product_id::text,
                        'code:' || LOWER(NULLIF(line.item_code, '')),
                        'name:' || LOWER(COALESCE(NULLIF(line.product_name, ''), NULLIF(line.name, '')))
                    ) AS product_key,
                    COALESCE(NULLIF(line.product_name, ''), NULLIF(line.name, ''), NULLIF(line.item_code, ''), 'Unmapped product') AS product_name,
                    COALESCE(line.item_code, '') AS item_code,
                    line.quantity,
                    line.price_subtotal AS revenue
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  AND invoice.company_id = ANY(%s)
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
            ),
            anchor_baskets AS (
                SELECT DISTINCT basket_id
                FROM scope_lines
                WHERE {anchor_sql}
            ),
            anchor_names AS (
                SELECT product_name, COUNT(DISTINCT basket_id) AS baskets
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND {anchor_sql}
                GROUP BY product_name
                ORDER BY baskets DESC, product_name
                LIMIT 1
            ),
            basket_companions AS (
                SELECT DISTINCT basket_id, product_key, product_name
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND NOT ({anchor_sql})
            ),
            companion_totals AS (
                SELECT product_key, product_name, COUNT(DISTINCT basket_id) AS co_baskets
                FROM basket_companions
                GROUP BY product_key, product_name
            ),
            base_totals AS (
                SELECT product_key, COUNT(DISTINCT basket_id) AS base_baskets
                FROM scope_lines
                GROUP BY product_key
            ),
            totals AS (
                SELECT
                    (SELECT COUNT(*) FROM anchor_baskets) AS anchor_baskets,
                    (SELECT COUNT(DISTINCT basket_id) FROM basket_companions) AS companion_baskets,
                    COUNT(DISTINCT basket_id) AS all_baskets,
                    COUNT(DISTINCT basket_id) FILTER (
                        WHERE partner_id IS NOT NULL
                          AND basket_id IN (SELECT basket_id FROM anchor_baskets)
                    ) AS identified_baskets,
                    COUNT(DISTINCT partner_id) FILTER (
                        WHERE partner_id IS NOT NULL
                          AND basket_id IN (SELECT basket_id FROM anchor_baskets)
                    ) AS identified_customers
                FROM scope_lines
            )
            SELECT
                companion.product_key,
                companion.product_name,
                companion.co_baskets,
                base.base_baskets,
                totals.anchor_baskets,
                totals.companion_baskets,
                totals.all_baskets,
                totals.identified_baskets,
                totals.identified_customers,
                (SELECT product_name FROM anchor_names) AS anchor_name
            FROM companion_totals companion
            JOIN base_totals base ON base.product_key = companion.product_key
            CROSS JOIN totals
            ORDER BY companion.co_baskets DESC, companion.product_name
            LIMIT %s
            """.format(anchor_sql=anchor_sql),
            [start, end, company_ids, *anchor_params, *anchor_params, *anchor_params, limit],
        )
        columns = [column[0] for column in self.env.cr.description]
        companions = [dict(zip(columns, row)) for row in self.env.cr.fetchall()]

        self.env.cr.execute(
            """
            WITH anchors AS (
                SELECT DISTINCT invoice.id
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  AND invoice.company_id = ANY(%s)
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
            )
            SELECT
                partner.id AS partner_id,
                partner.name,
                partner.email,
                partner.mobile,
                COUNT(DISTINCT invoice.id) AS baskets,
                SUM(invoice.amount_untaxed) AS revenue,
                MAX(invoice.invoice_date) AS last_purchase
            FROM legacy_invoice invoice
            JOIN anchors ON anchors.id = invoice.id
            JOIN res_partner partner ON partner.id = invoice.partner_id
            GROUP BY partner.id, partner.name, partner.email, partner.mobile
            ORDER BY baskets DESC, revenue DESC
            LIMIT 50
            """.format(anchor_sql=direct_anchor_sql),
            [start, end, company_ids, *direct_anchor_params],
        )
        customer_columns = [column[0] for column in self.env.cr.description]
        customers = [dict(zip(customer_columns, row)) for row in self.env.cr.fetchall()]

        self.env.cr.execute(
            """
            WITH anchors AS (
                SELECT DISTINCT invoice.id, COALESCE(invoice.payment_method_summary, invoice.payment_journal_summary, '') AS payment_text
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  AND invoice.company_id = ANY(%s)
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
            )
            SELECT
                CASE
                    WHEN LOWER(payment_text) LIKE '%%valu%%' THEN 'ValU'
                    WHEN LOWER(payment_text) LIKE '%%souh%%' THEN 'Souhoola'
                    WHEN LOWER(payment_text) SIMILAR TO '%%(cash|bank|card|visa)%%' THEN 'Cash / Card'
                    ELSE 'Other'
                END AS payment_group,
                COUNT(*) AS baskets
            FROM anchors
            GROUP BY payment_group
            ORDER BY baskets DESC
            """.format(anchor_sql=direct_anchor_sql),
            [start, end, company_ids, *direct_anchor_params],
        )
        payments = [{"name": row[0], "baskets": row[1]} for row in self.env.cr.fetchall()]
        return companions, customers, payments

    def _source_counts(self, query, start, end, entity):
        current_anchor_sql, current_anchor_params = self._anchor_clause(entity, query, source="current", scoped=False)
        legacy_anchor_sql, legacy_anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=False)
        counts = {"current": 0, "legacy": 0}
        company_ids = self._company_ids()
        self.env.cr.execute(
            """
            SELECT COUNT(DISTINCT move.id)
            FROM account_move_line line
            JOIN account_move move ON move.id = line.move_id
            JOIN product_product product ON product.id = line.product_id
            JOIN product_template template ON template.id = product.product_tmpl_id
            WHERE move.state = 'posted'
              AND move.move_type IN ('out_invoice', 'out_receipt')
              AND move.invoice_date BETWEEN %s AND %s
              AND move.company_id = ANY(%s)
              AND line.quantity > 0
              AND {anchor_sql}
            """.format(anchor_sql=current_anchor_sql),
            [start, end, company_ids, *current_anchor_params],
        )
        counts["current"] = self.env.cr.fetchone()[0]
        if self._has_table("legacy_invoice_line"):
            self.env.cr.execute(
                """
                SELECT COUNT(DISTINCT invoice.id)
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  AND invoice.company_id = ANY(%s)
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
                """.format(anchor_sql=legacy_anchor_sql),
                [start, end, company_ids, *legacy_anchor_params],
            )
            counts["legacy"] = self.env.cr.fetchone()[0]
        return counts

    def _coverage(self, counts, source_used, start, end):
        coverage = []
        for key in ("current", "legacy"):
            coverage.append(
                {
                    "key": key,
                    "label": self.SOURCE_LABELS[key],
                    "status": "active" if key == source_used else ("available" if counts[key] else "empty"),
                    "anchor_baskets": counts[key],
                }
            )
        coverage.append(
            {
                "key": "history",
                "label": "Full history",
                "status": "pending",
                "anchor_baskets": 0,
                "note": "Validated backfill coverage is shown explicitly; unverified periods are never implied complete.",
            }
        )
        return {
            "start_date": fields.Date.to_string(start),
            "end_date": fields.Date.to_string(end),
            "sources": coverage,
            "rule": "Best available source; Odoo 18 and Odoo 12 facts are not added together.",
        }

    @api.model
    def search_entities(self, query, limit=12):
        self._ensure_access()
        query = (query or "").strip()
        if len(query) < 2:
            return []
        limit = max(3, min(int(limit or 12), 30))
        output = []
        seen = set()

        def append_records(model_name, entity_type, result_limit):
            for record_id, name in self.env[model_name].sudo().name_search(
                query, operator="ilike", limit=result_limit
            ):
                key = f"{entity_type}:{record_id}"
                if key in seen:
                    continue
                seen.add(key)
                output.append(
                    {
                        "key": key,
                        "id": record_id,
                        "name": name,
                        "type": entity_type,
                        "source": "current",
                    }
                )

        base_quota = max(1, limit // 3)
        append_records("product.category", "category", base_quota)
        append_records("product.template", "product", base_quota)
        append_records("product.product", "variant", max(1, limit - (base_quota * 2)))
        if len(output) < limit and self._has_table("legacy_invoice_line"):
            self.env.cr.execute(
                """
                SELECT COALESCE(NULLIF(product_name, ''), NULLIF(name, ''), NULLIF(item_code, '')) AS label,
                       COUNT(*) AS line_count
                FROM legacy_invoice_line
                WHERE COALESCE(product_name, name, item_code, '') ILIKE %s
                GROUP BY label
                ORDER BY line_count DESC, label
                LIMIT %s
                """,
                [f"%{query}%", limit * 2],
            )
            for label, _line_count in self.env.cr.fetchall():
                if not label or label.strip().lower() in seen:
                    continue
                legacy_key = f"legacy-query:{label.strip().lower()}"
                if legacy_key in seen:
                    continue
                seen.add(legacy_key)
                output.append(
                    {
                        "key": legacy_key,
                        "id": 0,
                        "name": label,
                        "type": "query",
                        "source": "legacy",
                    }
                )
                if len(output) >= limit:
                    break
        return output[:limit]

    @api.model
    def search_products(self, query, limit=12):
        """Compatibility alias for clients deployed before hierarchical search."""
        return self.search_entities(query, limit)

    @api.model
    def get_product_360(self, query, start_date=None, end_date=None, source="auto", limit=20, entity=None):
        self._ensure_access()
        query = (query or "").strip()
        entity = self._normalize_entity(entity, query)
        if entity["type"] == "query" and len(query) < 2:
            raise UserError("Choose a product or enter at least two search characters.")
        start, end = self._date_range(start_date, end_date)
        counts = self._source_counts(query, start, end, entity)
        if source not in {"auto", "current", "legacy"}:
            source = "auto"
        if source == "auto":
            source_used = "legacy" if counts["legacy"] else "current"
        else:
            source_used = source
        if source_used == "legacy":
            companions, customers, payments = self._query_legacy(query, start, end, int(limit or 20), entity)
        else:
            companions, customers, payments = self._query_current(query, start, end, int(limit or 20), entity)

        first = companions[0] if companions else {}
        baskets = int(first.get("anchor_baskets") or counts.get(source_used) or 0)
        all_baskets = int(first.get("all_baskets") or 0)
        identified_baskets = int(first.get("identified_baskets") or 0)
        identified_customers = int(first.get("identified_customers") or 0)
        for row in companions:
            co_baskets = int(row.get("co_baskets") or 0)
            base_baskets = int(row.get("base_baskets") or 0)
            attach_rate = (co_baskets / baskets * 100.0) if baskets else 0.0
            base_rate = (base_baskets / all_baskets) if all_baskets else 0.0
            lift = ((co_baskets / baskets) / base_rate) if baskets and base_rate else 0.0
            row.update(
                {
                    "co_baskets": co_baskets,
                    "attach_rate": round(attach_rate, 2),
                    "lift": round(lift, 2),
                    "confidence": "High" if co_baskets >= 100 else ("Medium" if co_baskets >= 30 else "Exploratory"),
                    "signal": 4 if co_baskets >= 100 and lift >= 1.2 else (3 if co_baskets >= 30 else 2),
                }
            )
        companion_baskets = int(first.get("companion_baskets") or 0)
        payment_total = sum(int(row["baskets"]) for row in payments) or 1
        for row in payments:
            row["pct"] = round(int(row["baskets"]) / payment_total * 100.0, 2)
        for customer in customers:
            if isinstance(customer.get("last_purchase"), date):
                customer["last_purchase"] = fields.Date.to_string(customer["last_purchase"])
            customer["revenue"] = float(customer.get("revenue") or 0.0)

        top = companions[0] if companions else None
        display_name = entity["name"] if entity["type"] != "query" else (first.get("anchor_name") or query)
        grain_labels = {
            "category": "Category (including child categories)",
            "product": "Product family",
            "variant": "Exact variant / SKU",
            "query": "Search match",
        }
        recommendation = {
            "title": f"Bundle {display_name} + {top['product_name']}" if top else "Expand the date range",
            "rationale": "Highest attach volume with meaningful lift" if top else "No companion signal is available for this scope.",
            "reachable_baskets": int(top.get("co_baskets") or 0) if top else 0,
        }
        return {
            "product": {
                "name": display_name,
                "query": query,
                "type": entity["type"],
                "id": entity["id"],
                "grain_label": grain_labels[entity["type"]],
            },
            "summary": {
                "baskets": baskets,
                "companion_baskets": companion_baskets,
                "attach_rate": round(companion_baskets / baskets * 100.0, 2) if baskets else 0.0,
                "identified_baskets": identified_baskets,
                "identified_coverage": round(identified_baskets / baskets * 100.0, 2) if baskets else 0.0,
                "identified_customers": identified_customers,
            },
            "companions": companions,
            "customers": customers,
            "payment_mix": payments,
            "recommendation": recommendation,
            "coverage": self._coverage(counts, source_used, start, end),
            "source_requested": source,
            "source_used": source_used,
            "source_label": self.SOURCE_LABELS[source_used],
        }

    @api.model
    def export_product_insight(self, query, start_date=None, end_date=None, source="auto", entity=None):
        self._ensure_access()
        bundle = self.get_product_360(query, start_date, end_date, source, 100, entity)
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["Tradeline Product Intelligence", bundle["product"]["name"]])
        writer.writerow(["Data source", bundle["source_label"]])
        writer.writerow(["Analysis grain", bundle["product"]["grain_label"]])
        writer.writerow(["Date from", bundle["coverage"]["start_date"]])
        writer.writerow(["Date to", bundle["coverage"]["end_date"]])
        writer.writerow([])
        writer.writerow(["Companion", "Co-baskets", "Attach rate %", "Lift", "Confidence"])
        for row in bundle["companions"]:
            writer.writerow([row["product_name"], row["co_baskets"], row["attach_rate"], row["lift"], row["confidence"]])
        content = stream.getvalue().encode("utf-8-sig")
        filename = f"tradeline_product_intelligence_{fields.Date.today()}.csv"
        attachment = self.env["ir.attachment"].sudo().create(
            {
                "name": filename,
                "datas": base64.b64encode(content),
                "mimetype": "text/csv",
            }
        )
        return {"type": "ir.actions.act_url", "url": f"/web/content/{attachment.id}?download=true", "target": "self"}

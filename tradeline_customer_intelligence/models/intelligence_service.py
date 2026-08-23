from __future__ import annotations

import base64
import io
import re
from datetime import date
from math import sqrt

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools.misc import xlsxwriter


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

    def _normalize_filters(self, filters=None):
        filters = dict(filters or {})
        customer_type = filters.get("customer_type")
        if customer_type not in {"individual", "company"}:
            customer_type = "all"
        try:
            customer_company_id = int(filters.get("customer_company_id") or 0)
        except (TypeError, ValueError):
            customer_company_id = 0
        customer_company = self.env["res.partner"].sudo().browse(customer_company_id).exists()
        if customer_company_id and not customer_company:
            customer_company_id = 0
        return {
            "customer_type": customer_type,
            "customer_company_id": customer_company_id,
            "customer_company_name": customer_company.name if customer_company else "",
            "customer_company_vat": customer_company.vat if customer_company else "",
        }

    def _audience_sql(self, basket_alias, filters, source="current"):
        """Return a safe partner-population predicate shared by live and archive SQL."""
        filters = self._normalize_filters(filters)
        if source == "legacy":
            conditions = []
            params = []
            linked_company = (
                "EXISTS (SELECT 1 FROM res_partner audience_partner "
                "JOIN res_partner commercial ON commercial.id = audience_partner.commercial_partner_id "
                f"WHERE audience_partner.id = {basket_alias}.partner_id AND {{condition}})"
            )
            if filters["customer_type"] == "company":
                conditions.append(
                    f"(LOWER(COALESCE({basket_alias}.source_partner_type, '')) = 'company' OR "
                    + linked_company.format(condition="COALESCE(commercial.is_company, FALSE)")
                    + ")"
                )
            elif filters["customer_type"] == "individual":
                conditions.append(
                    f"(LOWER(COALESCE({basket_alias}.source_partner_type, '')) IN ('person', 'individual') OR "
                    + linked_company.format(condition="NOT COALESCE(commercial.is_company, FALSE)")
                    + ")"
                )
            if filters["customer_company_id"]:
                identity_conditions = [
                    linked_company.format(condition="commercial.id = %s"),
                ]
                params.append(filters["customer_company_id"])
                if filters["customer_company_vat"]:
                    identity_conditions.append(
                        f"REGEXP_REPLACE(UPPER(COALESCE({basket_alias}.source_partner_tax_id, '')), '[^A-Z0-9]+', '', 'g') = %s"
                    )
                    params.append(re.sub(r"[^A-Z0-9]+", "", filters["customer_company_vat"].upper()))
                if filters["customer_company_name"]:
                    identity_conditions.append(f"LOWER(TRIM(COALESCE({basket_alias}.source_partner_name, ''))) = %s")
                    params.append(filters["customer_company_name"].strip().lower())
                conditions.append(f"({' OR '.join(identity_conditions)})")
            if not conditions:
                return "", []
            return f" AND {' AND '.join(conditions)}", params

        conditions = []
        params = []
        if filters["customer_type"] == "company":
            conditions.append("COALESCE(commercial.is_company, FALSE)")
        elif filters["customer_type"] == "individual":
            conditions.append("NOT COALESCE(commercial.is_company, FALSE)")
        if filters["customer_company_id"]:
            conditions.append("commercial.id = %s")
            params.append(filters["customer_company_id"])
        if not conditions:
            return "", []
        return (
            " AND EXISTS ("
            "SELECT 1 FROM res_partner audience_partner "
            "JOIN res_partner commercial ON commercial.id = audience_partner.commercial_partner_id "
            f"WHERE audience_partner.id = {basket_alias}.partner_id AND {' AND '.join(conditions)}"
            ")",
            params,
        )

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
        prefixes = []
        if entity_type != "query":
            product_domain = {
                "variant": [("id", "=", entity_id)],
                "product": [("product_tmpl_id", "=", entity_id)],
                "category": [("product_tmpl_id.categ_id", "in", category_ids)],
            }[entity_type]
            variants = self.env["product.product"].sudo().with_context(active_test=False).search(product_domain)
            prefixes = sorted(
                {
                    self._code_prefix(variant.barcode or variant.default_code)
                    for variant in variants
                    if self._code_prefix(variant.barcode or variant.default_code)
                }
            )
        return {
            "type": entity_type,
            "id": entity_id,
            "name": name,
            "category_ids": category_ids,
            "prefixes": prefixes,
        }

    @staticmethod
    def _code_prefix(value, length=5):
        normalized = re.sub(r"[^A-Z0-9]+", "", (value or "").upper())
        return normalized[:length] or ""

    def _anchor_clause(self, entity, query, *, source, scoped=True):
        if source == "legacy" and entity["type"] != "query" and entity.get("prefixes"):
            item_code = "item_code" if scoped else "line.item_code"
            normalized_prefix = (
                f"LEFT(REGEXP_REPLACE(UPPER(COALESCE({item_code}, '')), '[^A-Z0-9]+', '', 'g'), 5)"
            )
            return f"{normalized_prefix} = ANY(%s)", [entity["prefixes"]]
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

    def _query_current(self, query, start, end, limit, entity, filters=None):
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source="current", scoped=True)
        direct_anchor_sql, direct_anchor_params = self._anchor_clause(entity, query, source="current", scoped=False)
        audience_sql, audience_params = self._audience_sql("move", filters)
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
                  {audience_sql}
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
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
            [
                start,
                end,
                company_ids,
                *audience_params,
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
                  {audience_sql}
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
                'partner:' || partner.id::text AS customer_key,
                partner.id AS partner_id,
                partner.name,
                partner.email,
                partner.mobile,
                COALESCE(commercial.is_company, FALSE) AS is_company,
                CASE WHEN commercial.is_company THEN commercial.name ELSE NULL END AS company_name,
                COUNT(DISTINCT lines.basket_id) AS baskets,
                SUM(lines.revenue) AS revenue,
                MAX(lines.invoice_date) AS last_purchase
            FROM scope_lines lines
            JOIN anchors ON anchors.basket_id = lines.basket_id
            JOIN res_partner partner ON partner.id = lines.partner_id
            JOIN res_partner commercial ON commercial.id = partner.commercial_partner_id
            GROUP BY partner.id, partner.name, partner.email, partner.mobile, commercial.is_company, commercial.name
            ORDER BY baskets DESC, revenue DESC
            LIMIT 50
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
            [start, end, company_ids, *audience_params, *anchor_params],
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
                  {audience_sql}
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
            """.format(anchor_sql=direct_anchor_sql, audience_sql=audience_sql),
            [start, end, company_ids, *audience_params, *direct_anchor_params],
        )
        payments = [{"name": row[0], "baskets": row[1]} for row in self.env.cr.fetchall()]
        return companions, customers, payments

    def _query_legacy(self, query, start, end, limit, entity, filters=None):
        if not self._has_table("legacy_invoice_line"):
            return [], [], []
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=True)
        direct_anchor_sql, direct_anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=False)
        audience_sql, audience_params = self._audience_sql("invoice", filters, source="legacy")
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
                        'source:' || line.product_source_id::text,
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
                  {audience_sql}
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
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
            [start, end, company_ids, *audience_params, *anchor_params, *anchor_params, *anchor_params, limit],
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
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
            ),
            customer_invoices AS (
                SELECT
                    CASE
                        WHEN partner.id IS NOT NULL THEN 'partner:' || partner.id::text
                        WHEN invoice.source_partner_id IS NOT NULL THEN 'legacy:' || invoice.source_partner_id::text
                        WHEN NULLIF(TRIM(invoice.source_partner_mobile), '') IS NOT NULL
                            THEN 'legacy-mobile:' || REGEXP_REPLACE(invoice.source_partner_mobile, '[^0-9+]', '', 'g')
                        WHEN NULLIF(TRIM(invoice.source_partner_name), '') IS NOT NULL
                            THEN 'legacy-name:' || LOWER(TRIM(invoice.source_partner_name))
                    END AS customer_key,
                    partner.id AS partner_id,
                    COALESCE(partner.name, NULLIF(invoice.source_partner_name, ''), 'Legacy customer') AS customer_name,
                    partner.email,
                    COALESCE(partner.mobile, NULLIF(invoice.source_partner_mobile, '')) AS mobile,
                    (
                        COALESCE(commercial.is_company, FALSE)
                        OR LOWER(COALESCE(invoice.source_partner_type, '')) = 'company'
                    ) AS is_company,
                    CASE
                        WHEN COALESCE(commercial.is_company, FALSE) THEN commercial.name
                        WHEN LOWER(COALESCE(invoice.source_partner_type, '')) = 'company' THEN invoice.source_partner_name
                    END AS company_name,
                    invoice.id AS basket_id,
                    invoice.amount_untaxed,
                    invoice.invoice_date
                FROM legacy_invoice invoice
                JOIN anchors ON anchors.id = invoice.id
                LEFT JOIN res_partner partner ON partner.id = invoice.partner_id
                LEFT JOIN res_partner commercial ON commercial.id = partner.commercial_partner_id
            )
            SELECT
                customer_key,
                MAX(partner_id) AS partner_id,
                MAX(customer_name) AS name,
                MAX(email) AS email,
                MAX(mobile) AS mobile,
                BOOL_OR(is_company) AS is_company,
                MAX(company_name) AS company_name,
                COUNT(DISTINCT basket_id) AS baskets,
                SUM(amount_untaxed) AS revenue,
                MAX(invoice_date) AS last_purchase
            FROM customer_invoices
            WHERE customer_key IS NOT NULL
            GROUP BY customer_key
            ORDER BY baskets DESC, revenue DESC
            LIMIT 50
            """.format(anchor_sql=direct_anchor_sql, audience_sql=audience_sql),
            [start, end, company_ids, *audience_params, *direct_anchor_params],
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
                  {audience_sql}
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
            """.format(anchor_sql=direct_anchor_sql, audience_sql=audience_sql),
            [start, end, company_ids, *audience_params, *direct_anchor_params],
        )
        payments = [{"name": row[0], "baskets": row[1]} for row in self.env.cr.fetchall()]
        return companions, customers, payments

    def _query_current_dimensions(self, query, start, end, entity, filters=None):
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source="current", scoped=False)
        audience_sql, audience_params = self._audience_sql("move", filters)
        self.env.cr.execute(
            """
            WITH anchor_lines AS (
                SELECT
                    move.id AS basket_id,
                    move.partner_id,
                    move.invoice_date,
                    line.price_subtotal AS revenue,
                    COALESCE(line.discount, 0.0) AS discount,
                    move.team_id,
                    move.invoice_user_id
                FROM account_move_line line
                JOIN account_move move ON move.id = line.move_id
                JOIN product_product product ON product.id = line.product_id
                JOIN product_template template ON template.id = product.product_tmpl_id
                WHERE move.state = 'posted'
                  AND move.move_type IN ('out_invoice', 'out_receipt')
                  AND move.invoice_date BETWEEN %s AND %s
                  AND move.company_id = ANY(%s)
                  {audience_sql}
                  AND line.quantity > 0
                  AND (line.display_type = 'product' OR line.display_type IS NULL)
                  AND {anchor_sql}
            ),
            basket_discount AS (
                SELECT basket_id, BOOL_OR(discount > 0) AS discounted
                FROM anchor_lines
                GROUP BY basket_id
            )
            SELECT
                (
                    SELECT JSONB_BUILD_OBJECT(
                        'baskets', COUNT(DISTINCT basket_id),
                        'identified_baskets', COUNT(DISTINCT basket_id) FILTER (WHERE partner_id IS NOT NULL),
                        'identified_customers', COUNT(DISTINCT partner_id) FILTER (WHERE partner_id IS NOT NULL)
                    )
                    FROM anchor_lines
                ) AS scope_summary,
                (
                    SELECT COALESCE(
                        JSONB_AGG(
                            JSONB_BUILD_OBJECT(
                                'period', TO_CHAR(month, 'YYYY-MM'),
                                'label', TO_CHAR(month, 'Mon YYYY'),
                                'baskets', baskets,
                                'revenue', revenue
                            ) ORDER BY month
                        ), '[]'::jsonb
                    )
                    FROM (
                        SELECT DATE_TRUNC('month', invoice_date)::date AS month,
                               COUNT(DISTINCT basket_id) AS baskets,
                               COALESCE(SUM(revenue), 0.0) AS revenue
                        FROM anchor_lines
                        GROUP BY DATE_TRUNC('month', invoice_date)::date
                    ) trend_rows
                ) AS trend,
                (
                    SELECT COALESCE(
                        JSONB_AGG(
                            JSONB_BUILD_OBJECT('name', name, 'baskets', baskets, 'revenue', revenue)
                            ORDER BY baskets DESC, name
                        ), '[]'::jsonb
                    )
                    FROM (
                        SELECT COALESCE(
                                   TO_JSONB(team.name)->>'en_US',
                                   TO_JSONB(team.name)->>'en',
                                   TO_JSONB(team.name)#>>'{{}}',
                                   'Unassigned'
                               ) AS name,
                               COUNT(DISTINCT lines.basket_id) AS baskets,
                               COALESCE(SUM(lines.revenue), 0.0) AS revenue
                        FROM anchor_lines lines
                        LEFT JOIN crm_team team ON team.id = lines.team_id
                        GROUP BY name
                        ORDER BY baskets DESC, name
                        LIMIT 8
                    ) store_rows
                ) AS store_mix,
                (
                    SELECT COALESCE(
                        JSONB_AGG(
                            JSONB_BUILD_OBJECT('name', name, 'baskets', baskets, 'revenue', revenue)
                            ORDER BY baskets DESC, name
                        ), '[]'::jsonb
                    )
                    FROM (
                        SELECT COALESCE(partner.name, 'Unassigned') AS name,
                               COUNT(DISTINCT lines.basket_id) AS baskets,
                               COALESCE(SUM(lines.revenue), 0.0) AS revenue
                        FROM anchor_lines lines
                        LEFT JOIN res_users users ON users.id = lines.invoice_user_id
                        LEFT JOIN res_partner partner ON partner.id = users.partner_id
                        GROUP BY COALESCE(partner.name, 'Unassigned')
                        ORDER BY baskets DESC, name
                        LIMIT 8
                    ) salesperson_rows
                ) AS salesperson_mix,
                (
                    SELECT COALESCE(
                        JSONB_AGG(
                            JSONB_BUILD_OBJECT(
                                'name', CASE WHEN discounted THEN 'Discounted basket' ELSE 'Full-price basket' END,
                                'baskets', baskets
                            ) ORDER BY discounted
                        ), '[]'::jsonb
                    )
                    FROM (
                        SELECT discounted, COUNT(*) AS baskets
                        FROM basket_discount
                        GROUP BY discounted
                    ) discount_rows
                ) AS discount_mix
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
            [start, end, self._company_ids(), *audience_params, *anchor_params],
        )
        row = self.env.cr.fetchone() or ({}, [], [], [], [])
        return {
            "scope_summary": row[0] or {},
            "trend": row[1] or [],
            "store_mix": row[2] or [],
            "salesperson_mix": row[3] or [],
            "discount_mix": row[4] or [],
            "channel_mix": [],
        }

    def _query_legacy_dimensions(self, query, start, end, entity, filters=None):
        if not self._has_table("legacy_invoice_line"):
            return {"scope_summary": {}, "trend": [], "store_mix": [], "salesperson_mix": [], "discount_mix": [], "channel_mix": []}
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=False)
        audience_sql, audience_params = self._audience_sql("invoice", filters, source="legacy")
        self.env.cr.execute(
            """
            WITH anchor_lines AS (
                SELECT
                    invoice.id AS basket_id,
                    CASE
                        WHEN invoice.partner_id IS NOT NULL THEN 'partner:' || invoice.partner_id::text
                        WHEN invoice.source_partner_id IS NOT NULL THEN 'legacy:' || invoice.source_partner_id::text
                        WHEN NULLIF(TRIM(invoice.source_partner_mobile), '') IS NOT NULL
                            THEN 'legacy-mobile:' || REGEXP_REPLACE(invoice.source_partner_mobile, '[^0-9+]', '', 'g')
                        WHEN NULLIF(TRIM(invoice.source_partner_name), '') IS NOT NULL
                            THEN 'legacy-name:' || LOWER(TRIM(invoice.source_partner_name))
                    END AS customer_key,
                    invoice.invoice_date,
                    line.price_subtotal AS revenue,
                    COALESCE(line.discount, 0.0) AS discount,
                    COALESCE(line.source_branch_name, invoice.source_team_name, 'Unassigned') AS store_name,
                    COALESCE(line.source_salesperson_name, invoice.source_sales_rep_name, invoice.source_user_name, 'Unassigned') AS salesperson_name,
                    COALESCE(line.source_channel, 'Unspecified') AS channel_name
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  AND invoice.company_id = ANY(%s)
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
            ),
            basket_discount AS (
                SELECT basket_id, BOOL_OR(discount > 0) AS discounted
                FROM anchor_lines
                GROUP BY basket_id
            )
            SELECT
                (
                    SELECT JSONB_BUILD_OBJECT(
                        'baskets', COUNT(DISTINCT basket_id),
                        'identified_baskets', COUNT(DISTINCT basket_id) FILTER (WHERE customer_key IS NOT NULL),
                        'identified_customers', COUNT(DISTINCT customer_key) FILTER (WHERE customer_key IS NOT NULL)
                    )
                    FROM anchor_lines
                ) AS scope_summary,
                (
                    SELECT COALESCE(
                        JSONB_AGG(
                            JSONB_BUILD_OBJECT(
                                'period', TO_CHAR(month, 'YYYY-MM'),
                                'label', TO_CHAR(month, 'Mon YYYY'),
                                'baskets', baskets,
                                'revenue', revenue
                            ) ORDER BY month
                        ), '[]'::jsonb
                    )
                    FROM (
                        SELECT DATE_TRUNC('month', invoice_date)::date AS month,
                               COUNT(DISTINCT basket_id) AS baskets,
                               COALESCE(SUM(revenue), 0.0) AS revenue
                        FROM anchor_lines
                        GROUP BY DATE_TRUNC('month', invoice_date)::date
                    ) trend_rows
                ) AS trend,
                (
                    SELECT COALESCE(
                        JSONB_AGG(JSONB_BUILD_OBJECT('name', store_name, 'baskets', baskets, 'revenue', revenue) ORDER BY baskets DESC, store_name),
                        '[]'::jsonb
                    )
                    FROM (
                        SELECT store_name, COUNT(DISTINCT basket_id) AS baskets, COALESCE(SUM(revenue), 0.0) AS revenue
                        FROM anchor_lines GROUP BY store_name ORDER BY baskets DESC, store_name LIMIT 8
                    ) store_rows
                ) AS store_mix,
                (
                    SELECT COALESCE(
                        JSONB_AGG(JSONB_BUILD_OBJECT('name', salesperson_name, 'baskets', baskets, 'revenue', revenue) ORDER BY baskets DESC, salesperson_name),
                        '[]'::jsonb
                    )
                    FROM (
                        SELECT salesperson_name, COUNT(DISTINCT basket_id) AS baskets, COALESCE(SUM(revenue), 0.0) AS revenue
                        FROM anchor_lines GROUP BY salesperson_name ORDER BY baskets DESC, salesperson_name LIMIT 8
                    ) salesperson_rows
                ) AS salesperson_mix,
                (
                    SELECT COALESCE(
                        JSONB_AGG(
                            JSONB_BUILD_OBJECT(
                                'name', CASE WHEN discounted THEN 'Discounted basket' ELSE 'Full-price basket' END,
                                'baskets', baskets
                            ) ORDER BY discounted
                        ), '[]'::jsonb
                    )
                    FROM (
                        SELECT discounted, COUNT(*) AS baskets FROM basket_discount GROUP BY discounted
                    ) discount_rows
                ) AS discount_mix,
                (
                    SELECT COALESCE(
                        JSONB_AGG(JSONB_BUILD_OBJECT('name', channel_name, 'baskets', baskets) ORDER BY baskets DESC, channel_name),
                        '[]'::jsonb
                    )
                    FROM (
                        SELECT channel_name, COUNT(DISTINCT basket_id) AS baskets
                        FROM anchor_lines GROUP BY channel_name ORDER BY baskets DESC, channel_name LIMIT 8
                    ) channel_rows
                ) AS channel_mix
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
            [start, end, self._company_ids(), *audience_params, *anchor_params],
        )
        row = self.env.cr.fetchone() or ({}, [], [], [], [], [])
        return {
            "scope_summary": row[0] or {},
            "trend": row[1] or [],
            "store_mix": row[2] or [],
            "salesperson_mix": row[3] or [],
            "discount_mix": row[4] or [],
            "channel_mix": row[5] or [],
        }

    def _source_counts(self, query, start, end, entity, filters=None):
        current_anchor_sql, current_anchor_params = self._anchor_clause(entity, query, source="current", scoped=False)
        legacy_anchor_sql, legacy_anchor_params = self._anchor_clause(entity, query, source="legacy", scoped=False)
        current_audience_sql, current_audience_params = self._audience_sql("move", filters)
        legacy_audience_sql, legacy_audience_params = self._audience_sql("invoice", filters, source="legacy")
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
              {audience_sql}
              AND line.quantity > 0
              AND {anchor_sql}
            """.format(anchor_sql=current_anchor_sql, audience_sql=current_audience_sql),
            [start, end, company_ids, *current_audience_params, *current_anchor_params],
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
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
                """.format(anchor_sql=legacy_anchor_sql, audience_sql=legacy_audience_sql),
                [start, end, company_ids, *legacy_audience_params, *legacy_anchor_params],
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
        comparison_available = self._has_table("legacy_current_product_history")
        coverage.append(
            {
                "key": "history",
                "label": "Product comparison history",
                "status": "available" if comparison_available else "pending",
                "anchor_baskets": 0,
                "note": (
                    "Full January–December 2025 monthly product facts are available and compared to Odoo 18 by the normalized five-character item-code prefix."
                    if comparison_available
                    else "The prefix-based legacy/current comparison view is not installed in this database."
                ),
            }
        )
        return {
            "start_date": fields.Date.to_string(start),
            "end_date": fields.Date.to_string(end),
            "sources": coverage,
            "rule": "Best available source; Odoo 18 and Odoo 12 facts are not added together.",
        }

    @api.model
    def get_legacy_comparison(self, query, entity=None):
        """Compare the full legacy fact history with live Odoo using Tradeline's prefix-5 item identity."""
        self._ensure_access()
        query = (query or "").strip()
        entity = self._normalize_entity(entity, query)
        if entity["type"] == "query" and len(query) < 2:
            raise UserError("Choose a product or enter at least two search characters.")
        if not self._has_table("legacy_current_product_history"):
            return {
                "available": False,
                "match_rule": "prefix5_only",
                "rule_label": "First 5 normalized item-code characters",
                "note": "The legacy/current product history view is not installed in this database.",
                "months": [],
            }

        if entity.get("prefixes"):
            scope_sql = "history.bucket_code_prefix5 = ANY(%s)"
            scope_params = [entity["prefixes"]]
        else:
            pattern = f"%{query}%"
            scope_sql = "(history.bucket_name ILIKE %s OR history.sample_item_code ILIKE %s)"
            scope_params = [pattern, pattern]

        self.env.cr.execute(
            """
            SELECT
                history.source_system,
                EXTRACT(MONTH FROM history.period_month)::integer AS month_number,
                MIN(history.period_month) AS period_month,
                COALESCE(SUM(history.sales_qty), 0.0) AS sales_qty,
                COALESCE(SUM(history.sales_amount), 0.0) AS sales_amount,
                COALESCE(SUM(history.return_qty), 0.0) AS return_qty,
                COALESCE(SUM(history.return_amount), 0.0) AS return_amount,
                COALESCE(SUM(history.discount_amount), 0.0) AS discount_amount,
                COALESCE(SUM(history.gross_sales_amount), 0.0) AS gross_sales_amount,
                COUNT(DISTINCT NULLIF(history.bucket_code_prefix5, '')) AS prefix_count,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT history.bucket_code_prefix5), NULL) AS observed_prefixes
            FROM legacy_current_product_history history
            WHERE {scope_sql}
              AND (
                    (history.source_system = 'legacy' AND history.period_month BETWEEN DATE '2025-01-01' AND DATE '2025-12-31')
                 OR (history.source_system = 'current' AND history.period_month >= DATE '2026-01-01')
              )
            GROUP BY history.source_system, EXTRACT(MONTH FROM history.period_month)::integer
            ORDER BY month_number, history.source_system
            """.format(scope_sql=scope_sql),
            scope_params,
        )
        columns = [column[0] for column in self.env.cr.description]
        rows = [dict(zip(columns, row)) for row in self.env.cr.fetchall()]

        self.env.cr.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT period_month), MIN(period_month), MAX(period_month)
            FROM legacy_product_month_fact
            WHERE period_month BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
            """
        )
        fact_rows, fact_months, fact_start, fact_end = self.env.cr.fetchone() or (0, 0, None, None)

        by_source_month = {(row["source_system"], row["month_number"]): row for row in rows}
        current_months = [row["month_number"] for row in rows if row["source_system"] == "current"]
        comparable_month = max(current_months, default=0)

        def sum_metric(source, metric, through_month=12):
            return float(
                sum(
                    float(row.get(metric) or 0.0)
                    for row in rows
                    if row["source_system"] == source and row["month_number"] <= through_month
                )
            )

        def pct_delta(current_value, legacy_value):
            if not legacy_value:
                return None
            return round((current_value - legacy_value) / legacy_value * 100.0, 2)

        legacy_full_qty = sum_metric("legacy", "sales_qty")
        legacy_full_amount = sum_metric("legacy", "sales_amount")
        legacy_ytd_qty = sum_metric("legacy", "sales_qty", comparable_month) if comparable_month else 0.0
        legacy_ytd_amount = sum_metric("legacy", "sales_amount", comparable_month) if comparable_month else 0.0
        current_ytd_qty = sum_metric("current", "sales_qty", comparable_month) if comparable_month else 0.0
        current_ytd_amount = sum_metric("current", "sales_amount", comparable_month) if comparable_month else 0.0

        month_labels = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        months = []
        for month_number, label in enumerate(month_labels, 1):
            legacy = by_source_month.get(("legacy", month_number), {})
            current = by_source_month.get(("current", month_number), {})
            legacy_amount = float(legacy.get("sales_amount") or 0.0)
            current_amount = float(current.get("sales_amount") or 0.0)
            months.append(
                {
                    "month": month_number,
                    "label": label,
                    "legacy_qty": float(legacy.get("sales_qty") or 0.0),
                    "legacy_amount": legacy_amount,
                    "current_qty": float(current.get("sales_qty") or 0.0),
                    "current_amount": current_amount,
                    "amount_delta_pct": pct_delta(current_amount, legacy_amount),
                    "has_current": bool(current),
                    "has_legacy": bool(legacy),
                }
            )

        observed = {
            source: sorted(
                {
                    prefix
                    for row in rows
                    if row["source_system"] == source
                    for prefix in (row.get("observed_prefixes") or [])
                    if prefix
                }
            )
            for source in ("legacy", "current")
        }
        selected_prefixes = entity.get("prefixes") or sorted(set(observed["legacy"]) | set(observed["current"]))
        shared_prefixes = sorted(set(observed["legacy"]) & set(observed["current"]))
        return {
            "available": True,
            "match_rule": "prefix5_only",
            "rule_label": "First 5 normalized item-code characters",
            "normalization": "Use the first available item code, remove non-alphanumeric characters, uppercase it, then take the first five characters.",
            "scope_type": entity["type"],
            "scope_name": entity["name"] if entity["type"] != "query" else query,
            "prefixes": selected_prefixes,
            "prefix_count": len(selected_prefixes),
            "shared_prefix_count": len(shared_prefixes),
            "legacy_only_prefix_count": len(set(observed["legacy"]) - set(observed["current"])),
            "current_only_prefix_count": len(set(observed["current"]) - set(observed["legacy"])),
            "legacy": {
                "full_year_qty": legacy_full_qty,
                "full_year_amount": legacy_full_amount,
                "comparable_ytd_qty": legacy_ytd_qty,
                "comparable_ytd_amount": legacy_ytd_amount,
            },
            "current": {
                "through_month": comparable_month,
                "ytd_qty": current_ytd_qty,
                "ytd_amount": current_ytd_amount,
            },
            "delta": {
                "qty_pct": pct_delta(current_ytd_qty, legacy_ytd_qty),
                "amount_pct": pct_delta(current_ytd_amount, legacy_ytd_amount),
            },
            "months": months,
            "coverage": {
                "legacy_fact_rows": int(fact_rows or 0),
                "legacy_fact_months": int(fact_months or 0),
                "legacy_fact_start": fields.Date.to_string(fact_start) if fact_start else "",
                "legacy_fact_end": fields.Date.to_string(fact_end) if fact_end else "",
                "legacy_level": "Monthly product facts",
                "basket_level": "Migrated invoice lines (currently December 2025 pilot)",
            },
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

        def append_item(record, entity_type, subtitle, item_code="", match_hint=""):
            key = f"{entity_type}:{record.id}"
            if key in seen or len(output) >= limit:
                return
            seen.add(key)
            output.append(
                {
                    "key": key,
                    "id": record.id,
                    "name": record.display_name,
                    "type": entity_type,
                    "source": "current",
                    "subtitle": subtitle,
                    "item_code": item_code or "",
                    "match_hint": match_hint or "",
                }
            )

        variant_domain = [
            "|", "|", "|",
            ("name", "ilike", query),
            ("product_tmpl_id.name", "ilike", query),
            ("barcode", "ilike", query),
            ("default_code", "ilike", query),
        ]
        variants = self.env["product.product"].sudo().with_context(active_test=False).search(
            variant_domain, limit=max(limit * 2, 20)
        )
        templates = self.env["product.template"].sudo().with_context(active_test=False).search(
            [("name", "ilike", query)], limit=max(limit, 12)
        )
        template_ids = set(templates.ids)
        templates |= variants.mapped("product_tmpl_id").filtered(lambda template: template.id not in template_ids)

        normalized_query = re.sub(r"[^A-Z0-9]+", "", query.upper())
        product_quota = max(2, limit // 3)
        for template in templates[:product_quota]:
            variant_count = len(template.with_context(active_test=False).product_variant_ids)
            category_name = template.categ_id.display_name or "Uncategorized"
            append_item(
                template,
                "product",
                f"{category_name} · {variant_count} variant{'s' if variant_count != 1 else ''}",
            )

        variant_quota = max(3, limit - product_quota - 2)
        for variant in variants[:variant_quota]:
            item_code = variant.barcode or variant.default_code or ""
            normalized_code = re.sub(r"[^A-Z0-9]+", "", item_code.upper())
            match_hint = "Matched by item code" if normalized_query and normalized_query in normalized_code else ""
            append_item(
                variant,
                "variant",
                f"{variant.categ_id.display_name or 'Uncategorized'}" + (f" · {item_code}" if item_code else ""),
                item_code,
                match_hint,
            )

        categories = self.env["product.category"].sudo().search([("name", "ilike", query)], limit=3)
        for category in categories:
            family_count = self.env["product.template"].sudo().with_context(active_test=False).search_count(
                [("categ_id", "child_of", category.id)]
            )
            append_item(category, "category", f"{family_count} product families")

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
                        "subtitle": "Legacy archive text match · basket search",
                        "item_code": "",
                        "match_hint": "",
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
    def get_filter_options(self):
        """Small, human-readable filter dictionary for the command bar."""
        self._ensure_access()
        companies = self.env["res.partner"].sudo().search(
            [("is_company", "=", True), ("active", "=", True), ("customer_rank", ">", 0)],
            order="name",
            limit=200,
        )
        return {
            "customer_types": [
                {"key": "all", "label": "All customers"},
                {"key": "individual", "label": "Individuals"},
                {"key": "company", "label": "Companies"},
            ],
            "customer_companies": [{"id": company.id, "name": company.display_name} for company in companies],
        }

    @api.model
    def get_product_360(self, query, start_date=None, end_date=None, source="auto", limit=20, entity=None, filters=None):
        self._ensure_access()
        query = (query or "").strip()
        entity = self._normalize_entity(entity, query)
        filters = self._normalize_filters(filters)
        if entity["type"] == "query" and len(query) < 2:
            raise UserError("Choose a product or enter at least two search characters.")
        start, end = self._date_range(start_date, end_date)
        counts = self._source_counts(query, start, end, entity, filters)
        if source not in {"auto", "current", "legacy"}:
            source = "auto"
        if source == "auto":
            source_used = "legacy" if counts["legacy"] else "current"
        else:
            source_used = source
        if source_used == "legacy":
            companions, customers, payments = self._query_legacy(query, start, end, int(limit or 20), entity, filters)
            dimensions = self._query_legacy_dimensions(query, start, end, entity, filters)
        else:
            companions, customers, payments = self._query_current(query, start, end, int(limit or 20), entity, filters)
            dimensions = self._query_current_dimensions(query, start, end, entity, filters)

        first = companions[0] if companions else {}
        scope_summary = dimensions.get("scope_summary") or {}
        baskets = int(scope_summary.get("baskets") or first.get("anchor_baskets") or counts.get(source_used) or 0)
        all_baskets = int(first.get("all_baskets") or 0)
        identified_baskets = int(scope_summary.get("identified_baskets") or first.get("identified_baskets") or 0)
        identified_customers = int(scope_summary.get("identified_customers") or first.get("identified_customers") or 0)
        for row in companions:
            co_baskets = int(row.get("co_baskets") or 0)
            base_baskets = int(row.get("base_baskets") or 0)
            attach_rate = (co_baskets / baskets * 100.0) if baskets else 0.0
            base_rate = (base_baskets / all_baskets) if all_baskets else 0.0
            lift = ((co_baskets / baskets) / base_rate) if baskets and base_rate else 0.0
            probability = co_baskets / baskets if baskets else 0.0
            if baskets:
                z = 1.96
                denominator = 1.0 + (z * z / baskets)
                center = (probability + (z * z / (2.0 * baskets))) / denominator
                margin = z * sqrt(
                    (probability * (1.0 - probability) / baskets) + (z * z / (4.0 * baskets * baskets))
                ) / denominator
                confidence_low = max(0.0, center - margin) * 100.0
                confidence_high = min(1.0, center + margin) * 100.0
            else:
                confidence_low = confidence_high = 0.0
            opportunity_score = min(
                100.0,
                (min(attach_rate, 100.0) * 0.45)
                + (min(lift, 4.0) / 4.0 * 30.0)
                + (min(co_baskets, 100) / 100.0 * 25.0),
            )
            row.update(
                {
                    "co_baskets": co_baskets,
                    "attach_rate": round(attach_rate, 2),
                    "lift": round(lift, 2),
                    "attach_ci_low": round(confidence_low, 2),
                    "attach_ci_high": round(confidence_high, 2),
                    "opportunity_score": round(opportunity_score, 1),
                    "confidence": "High" if co_baskets >= 100 else ("Medium" if co_baskets >= 30 else "Exploratory"),
                    "signal": 4 if co_baskets >= 100 and lift >= 1.2 else (3 if co_baskets >= 30 else 2),
                }
            )
        companion_baskets = int(first.get("companion_baskets") or 0)
        payment_total = sum(int(row["baskets"]) for row in payments) or 1
        for row in payments:
            row["pct"] = round(int(row["baskets"]) / payment_total * 100.0, 2)
        max_customer_revenue = max((float(row.get("revenue") or 0.0) for row in customers), default=1.0) or 1.0
        max_customer_baskets = max((int(row.get("baskets") or 0) for row in customers), default=1) or 1
        period_days = max((end - start).days, 1)
        customer_segment_counts = {"Priority": 0, "Growth": 0, "Core": 0}
        for customer in customers:
            last_purchase = customer.get("last_purchase")
            recency_days = max((end - last_purchase).days, 0) if isinstance(last_purchase, date) else period_days
            revenue = float(customer.get("revenue") or 0.0)
            customer_baskets = int(customer.get("baskets") or 0)
            contactable = bool(customer.get("email") or customer.get("mobile"))
            recency_component = max(0.0, 1.0 - (recency_days / period_days))
            score = round(
                (revenue / max_customer_revenue * 40.0)
                + (customer_baskets / max_customer_baskets * 30.0)
                + (recency_component * 25.0)
                + (5.0 if contactable else 0.0)
            )
            segment = "Priority" if score >= 70 else ("Growth" if score >= 45 else "Core")
            customer_segment_counts[segment] += 1
            customer.update(
                {
                    "last_purchase": fields.Date.to_string(last_purchase) if isinstance(last_purchase, date) else "",
                    "revenue": revenue,
                    "avg_basket_value": round(revenue / customer_baskets, 2) if customer_baskets else 0.0,
                    "recency_days": recency_days,
                    "activation_score": min(score, 100),
                    "segment": segment,
                    "contact_status": "Email + mobile" if customer.get("email") and customer.get("mobile") else (
                        "Email" if customer.get("email") else ("Mobile" if customer.get("mobile") else "Unreachable")
                    ),
                }
            )

        trend = dimensions.get("trend") or []
        trend_change = 0.0
        if len(trend) >= 2:
            previous_baskets = int(trend[-2].get("baskets") or 0)
            current_baskets = int(trend[-1].get("baskets") or 0)
            trend_change = ((current_baskets - previous_baskets) / previous_baskets * 100.0) if previous_baskets else 0.0
        dimensions["trend_change_pct"] = round(trend_change, 2)
        dimensions["trend_direction"] = "up" if trend_change > 0 else ("down" if trend_change < 0 else "flat")

        top = max(companions, key=lambda row: row.get("opportunity_score", 0.0), default=None)
        display_name = entity["name"] if entity["type"] != "query" else query
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
            "customer_segments": [
                {"name": name, "customers": customer_segment_counts[name]}
                for name in ("Priority", "Growth", "Core")
            ],
            "payment_mix": payments,
            "dimensions": dimensions,
            "recommendation": recommendation,
            "coverage": self._coverage(counts, source_used, start, end),
            "source_requested": source,
            "source_used": source_used,
            "source_label": self.SOURCE_LABELS[source_used],
            "filters": {
                "customer_type": filters["customer_type"],
                "customer_company_id": filters["customer_company_id"],
            },
        }

    def _evidence_anchor_domain(self, entity, query, source):
        line_prefix = "invoice_line_ids" if source == "current" else "line_ids"
        if source == "current":
            if entity["type"] == "variant":
                return [(f"{line_prefix}.product_id", "=", entity["id"])]
            if entity["type"] == "product":
                return [(f"{line_prefix}.product_id.product_tmpl_id", "=", entity["id"])]
            if entity["type"] == "category":
                return [(f"{line_prefix}.product_id.product_tmpl_id.categ_id", "in", entity["category_ids"])]
            return expression.OR(
                [
                    [(f"{line_prefix}.product_id.product_tmpl_id.name", "ilike", query)],
                    [(f"{line_prefix}.product_id.default_code", "ilike", query)],
                    [(f"{line_prefix}.name", "ilike", query)],
                ]
            )
        if entity.get("prefixes"):
            return expression.OR([[(f"{line_prefix}.item_code", "ilike", f"{prefix}%")] for prefix in entity["prefixes"]])
        return expression.OR(
            [
                [(f"{line_prefix}.product_name", "ilike", query)],
                [(f"{line_prefix}.item_code", "ilike", query)],
                [(f"{line_prefix}.name", "ilike", query)],
            ]
        )

    @api.model
    def open_evidence(
        self,
        query,
        start_date=None,
        end_date=None,
        source="auto",
        entity=None,
        companion_key=None,
        filters=None,
    ):
        """Open the actual live/archive invoices supporting the displayed metric."""
        self._ensure_access()
        query = (query or "").strip()
        entity = self._normalize_entity(entity, query)
        filters = self._normalize_filters(filters)
        start, end = self._date_range(start_date, end_date)
        counts = self._source_counts(query, start, end, entity, filters)
        source_used = source if source in {"current", "legacy"} else ("legacy" if counts["legacy"] else "current")
        current_customer_domain = []
        if filters["customer_type"] == "company":
            current_customer_domain.append(("partner_id.commercial_partner_id.is_company", "=", True))
        elif filters["customer_type"] == "individual":
            current_customer_domain.append(("partner_id.commercial_partner_id.is_company", "=", False))
        if filters["customer_company_id"]:
            current_customer_domain.append(("partner_id.commercial_partner_id", "=", filters["customer_company_id"]))

        if source_used == "legacy":
            legacy_customer_domain = []
            if filters["customer_type"] == "company":
                legacy_customer_domain.append(("source_partner_type", "=", "company"))
            elif filters["customer_type"] == "individual":
                legacy_customer_domain.append(("source_partner_type", "in", ["person", "individual"]))
            if filters["customer_company_id"]:
                identity_domains = [[("partner_id.commercial_partner_id", "=", filters["customer_company_id"])]]
                if filters["customer_company_vat"]:
                    identity_domains.append([("source_partner_tax_id", "ilike", filters["customer_company_vat"])])
                if filters["customer_company_name"]:
                    identity_domains.append([("source_partner_name", "=ilike", filters["customer_company_name"])])
                legacy_customer_domain += expression.OR(identity_domains)
            domain = [
                ("invoice_date", ">=", fields.Date.to_string(start)),
                ("invoice_date", "<=", fields.Date.to_string(end)),
                ("company_id", "in", self._company_ids()),
                ("invoice_type", "=", "out_invoice"),
                ("state", "!=", "cancel"),
            ] + legacy_customer_domain + self._evidence_anchor_domain(entity, query, "legacy")
            if companion_key:
                key = str(companion_key)
                if key.startswith("product:") and key.split(":", 1)[1].isdigit():
                    domain.append(("line_ids.product_id", "=", int(key.split(":", 1)[1])))
                elif key.startswith("source:") and key.split(":", 1)[1].isdigit():
                    domain.append(("line_ids.product_source_id", "=", int(key.split(":", 1)[1])))
                elif key.startswith("code:"):
                    domain.append(("line_ids.item_code", "ilike", key.split(":", 1)[1]))
                elif key.startswith("name:"):
                    domain.append(("line_ids.product_name", "ilike", key.split(":", 1)[1]))
            return {
                "type": "ir.actions.act_window",
                "name": f"Evidence · {entity['name'] or query} · Odoo 12",
                "res_model": "legacy.invoice",
                "view_mode": "list,form",
                "views": [(False, "list"), (False, "form")],
                "domain": domain,
                "target": "current",
                "context": {"search_default_group_by_invoice_date": 1},
            }

        domain = [
            ("state", "=", "posted"),
            ("move_type", "in", ["out_invoice", "out_receipt"]),
            ("invoice_date", ">=", fields.Date.to_string(start)),
            ("invoice_date", "<=", fields.Date.to_string(end)),
            ("company_id", "in", self._company_ids()),
        ] + current_customer_domain + self._evidence_anchor_domain(entity, query, "current")
        if companion_key and str(companion_key).isdigit():
            domain.append(("invoice_line_ids.product_id", "=", int(companion_key)))
        return {
            "type": "ir.actions.act_window",
            "name": f"Evidence · {entity['name'] or query} · Odoo 18",
            "res_model": "account.move",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": domain,
            "target": "current",
            "context": {"search_default_group_by_invoice_date": 1},
        }

    @api.model
    def export_product_insight(
        self,
        query,
        start_date=None,
        end_date=None,
        source="auto",
        entity=None,
        filters=None,
        export_mode="current_view",
    ):
        self._ensure_access()
        if export_mode not in {"current_view", "detail_rows", "customers", "legacy_live"}:
            export_mode = "current_view"
        bundle = self.get_product_360(query, start_date, end_date, source, 100, entity, filters)
        comparison = self.get_legacy_comparison(query, entity)
        stream = io.BytesIO()
        workbook = xlsxwriter.Workbook(stream, {"in_memory": True})
        title = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#123C2A"})
        section = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#17663C", "border": 1})
        header = workbook.add_format({"bold": True, "font_color": "#123C2A", "bg_color": "#EAF5ED", "border": 1})
        cell = workbook.add_format({"border": 1, "border_color": "#D8E0DA"})
        money = workbook.add_format({"border": 1, "border_color": "#D8E0DA", "num_format": '#,##0.00 "EGP"'})
        percent = workbook.add_format({"border": 1, "border_color": "#D8E0DA", "num_format": "0.00%"})

        def write_table(sheet, row, headers, rows, formats=None):
            for column, label in enumerate(headers):
                sheet.write(row, column, label, header)
            for row_offset, values in enumerate(rows, start=1):
                for column, value in enumerate(values):
                    sheet.write(row + row_offset, column, value, (formats or {}).get(column, cell))
            sheet.autofilter(row, 0, row + len(rows), len(headers) - 1)
            return row + len(rows) + 2

        if export_mode in {"current_view", "detail_rows"}:
            sheet = workbook.add_worksheet("Executive view")
            sheet.set_column("A:A", 34)
            sheet.set_column("B:H", 20)
            sheet.write(0, 0, "Tradeline Product Intelligence", title)
            metadata = [
                ("Scope", bundle["product"]["name"]),
                ("Analysis grain", bundle["product"]["grain_label"]),
                ("Source", bundle["source_label"]),
                ("Date range", f"{bundle['coverage']['start_date']} to {bundle['coverage']['end_date']}"),
                ("Customer type", bundle["filters"]["customer_type"]),
                ("Baskets", bundle["summary"]["baskets"]),
                ("Companion attach rate", bundle["summary"]["attach_rate"] / 100.0),
                ("Identified customers", bundle["summary"]["identified_customers"]),
            ]
            sheet.write(2, 0, "Evidence-backed summary", section)
            for index, (label, value) in enumerate(metadata, start=3):
                sheet.write(index, 0, label, header)
                sheet.write(index, 1, value, percent if label == "Companion attach rate" else cell)
            companion_rows = [
                [row["product_name"], row["co_baskets"], row["attach_rate"] / 100.0, row["lift"], row["confidence"], row["opportunity_score"]]
                for row in bundle["companions"]
            ]
            write_table(sheet, 13, ["Companion", "Co-baskets", "Attach rate", "Lift", "Confidence", "Decision score"], companion_rows, {2: percent})

        if export_mode in {"current_view", "detail_rows", "customers"}:
            sheet = workbook.add_worksheet("Customers")
            sheet.set_column("A:C", 28)
            sheet.set_column("D:J", 18)
            customer_rows = [
                [
                    customer["name"],
                    "Company" if customer.get("is_company") else "Individual",
                    customer.get("company_name") or "",
                    customer["segment"],
                    customer["activation_score"],
                    customer["baskets"],
                    customer["revenue"],
                    customer["last_purchase"],
                    customer["email"] or "",
                    customer["mobile"] or "",
                ]
                for customer in bundle["customers"]
            ]
            write_table(sheet, 0, ["Customer", "Customer type", "Company", "Segment", "Activation score", "Baskets", "Observed value", "Last purchase", "Email", "Mobile"], customer_rows, {6: money})

        if export_mode in {"current_view", "legacy_live"}:
            sheet = workbook.add_worksheet("Legacy vs Live")
            sheet.set_column("A:A", 22)
            sheet.set_column("B:F", 20)
            sheet.write(0, 0, "Legacy vs Odoo 18", title)
            if comparison.get("available"):
                sheet.write(2, 0, "Cross-version identity", header)
                sheet.write(2, 1, comparison["rule_label"], cell)
                sheet.write(3, 0, "Compared prefixes", header)
                sheet.write(3, 1, comparison["prefix_count"], cell)
                rows = [
                    [period["label"], period["legacy_qty"], period["legacy_amount"], period["current_qty"], period["current_amount"], period.get("amount_delta_pct")]
                    for period in comparison["months"]
                ]
                write_table(sheet, 6, ["Month", "Odoo 12 units", "Odoo 12 revenue", "Odoo 18 units", "Odoo 18 revenue", "Revenue change %"], rows, {2: money, 4: money})
            else:
                sheet.write(2, 0, comparison.get("note") or "Comparison data is unavailable.")

        workbook.close()
        content = stream.getvalue()
        filename = f"tradeline_intelligence_{export_mode}_{fields.Date.today()}.xlsx"
        attachment = self.env["ir.attachment"].sudo().create(
            {
                "name": filename,
                "datas": base64.b64encode(content),
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        )
        return {"type": "ir.actions.act_url", "url": f"/web/content/{attachment.id}?download=true", "target": "self"}

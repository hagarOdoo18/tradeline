from __future__ import annotations

import base64
import io
import re
from collections import defaultdict
from datetime import date, timedelta
from math import sqrt

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools.misc import xlsxwriter


class TradelineCustomerIntelligenceService(models.AbstractModel):
    _name = "tradeline.customer.intelligence.service"
    _description = "Tradeline Customer Intelligence Service"

    SOURCE_LABELS = {
        "current": "Current operations",
        "legacy": "Historical sales",
        "unified": "Unified sales history",
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

    def _unified_source_periods(self, start, end):
        """Assign every date to one authoritative ledger at the historical cutover."""
        if not self._has_table("legacy_invoice"):
            return {
                "legacy": (start, end),
                "current": (start, end),
                "cutover_date": None,
            }
        self.env.cr.execute(
            """
            SELECT MAX(invoice_date)
            FROM legacy_invoice
            WHERE invoice_type = 'out_invoice'
              AND state <> 'cancel'
            """
        )
        cutover = self.env.cr.fetchone()[0]
        if not cutover:
            return {
                "legacy": (start, end),
                "current": (start, end),
                "cutover_date": None,
            }
        return {
            "legacy": (start, min(end, cutover)),
            "current": (max(start, cutover + timedelta(days=1)), end),
            "cutover_date": cutover,
        }

    @classmethod
    def _transport_safe(cls, value):
        """Use Odoo's API convention (False) for nullable scalar values."""
        if value is None:
            return False
        if isinstance(value, dict):
            return {key: cls._transport_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._transport_safe(item) for item in value]
        return value

    def _date_range(self, start_date=None, end_date=None):
        today = fields.Date.context_today(self)
        start = fields.Date.to_date(start_date) if start_date else today.replace(day=1)
        end = fields.Date.to_date(end_date) if end_date else today
        if start > end:
            start, end = end, start
        return start, end

    def _company_ids(self, filters=None):
        """Resolve the operating-company scope without trusting client supplied ids."""
        allowed_ids = self.env.user.company_ids.ids or [self.env.company.id]
        filters = filters or {}
        try:
            company_id = int(filters.get("operating_company_id") or 0)
        except (TypeError, ValueError):
            company_id = 0
        return [company_id] if company_id in allowed_ids else allowed_ids

    def _legacy_business_sql(self, invoice_alias, filters=None):
        """Scope legacy invoices using preserved Odoo 12 business markers.

        The archive target ``company_id`` records the Odoo 18 owner of the imported
        row; it does not preserve the Odoo 12 Tradeline/XPRS business split.  The
        source journal, team and invoice references do preserve that distinction.
        """
        filters = self._normalize_filters(filters)
        company_id = filters["operating_company_id"]
        if not company_id:
            return "", []
        company_name = (filters["operating_company_name"] or "").strip().lower()
        marker_fields = (
            "source_journal_name",
            "source_journal_code",
            "source_team_name",
            "number",
            "source_name",
            "source_reference_number",
        )
        marker_parts = []
        marker_params = []
        for field_name in marker_fields:
            marker_parts.extend(
                [
                    f"COALESCE({invoice_alias}.{field_name}, '') ILIKE %s",
                    f"COALESCE({invoice_alias}.{field_name}, '') ILIKE %s",
                ]
            )
            marker_params.extend(["%xprs%", "%-x/%"])
        marker_sql = f"({' OR '.join(marker_parts)})"
        if "xprs" in company_name:
            return f" AND {marker_sql}", marker_params
        if "tradeline" in company_name:
            return f" AND NOT {marker_sql}", marker_params
        # Do not silently attribute legacy invoices to an unknown operating company.
        return " AND FALSE", []

    def _legacy_business_domain(self, filters=None):
        filters = self._normalize_filters(filters)
        company_id = filters["operating_company_id"]
        if not company_id:
            return []
        company_name = (filters["operating_company_name"] or "").strip().lower()
        marker_fields = (
            "source_journal_name",
            "source_journal_code",
            "source_team_name",
            "number",
            "source_name",
            "source_reference_number",
        )
        marker_domains = []
        for field_name in marker_fields:
            marker_domains.extend(
                [[(field_name, "ilike", "xprs")], [(field_name, "ilike", "-x/")]]
            )
        xprs_domain = expression.OR(marker_domains)
        if "xprs" in company_name:
            return xprs_domain
        if "tradeline" in company_name:
            return ["!"] + xprs_domain
        return [("id", "=", 0)]

    def _normalize_filters(self, filters=None):
        filters = dict(filters or {})
        customer_type = filters.get("customer_type")
        if customer_type not in {"individual", "company"}:
            customer_type = "all"
        try:
            customer_company_id = int(filters.get("customer_company_id") or 0)
        except (TypeError, ValueError):
            customer_company_id = 0
        try:
            operating_company_id = int(filters.get("operating_company_id") or 0)
        except (TypeError, ValueError):
            operating_company_id = 0
        allowed_company_ids = self.env.user.company_ids.ids or [self.env.company.id]
        if operating_company_id not in allowed_company_ids:
            operating_company_id = 0
        operating_company = self.env["res.company"].sudo().browse(operating_company_id).exists()
        customer_company = self.env["res.partner"].sudo().browse(customer_company_id).exists()
        if customer_company_id and not customer_company:
            customer_company_id = 0
        return {
            "customer_type": customer_type,
            "customer_company_id": customer_company_id,
            "customer_company_name": customer_company.name if customer_company else "",
            "customer_company_vat": customer_company.vat if customer_company else "",
            "operating_company_id": operating_company_id,
            "operating_company_name": operating_company.name if operating_company else "",
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
        entity_type = (
            entity.get("type")
            if entity.get("type") in {"category", "product", "variant", "legacy_variant"}
            else "query"
        )
        try:
            entity_id = int(entity.get("id") or 0) if entity_type != "query" else 0
        except (TypeError, ValueError):
            entity_id = 0
        name = (entity.get("name") or query or "").strip()
        if entity_type in {"category", "product", "variant"}:
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
        if entity_type in {"category", "product", "variant"}:
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
        elif entity_type == "legacy_variant":
            prefix = self._code_prefix(entity.get("prefix5") or entity.get("item_code"))
            if not prefix:
                entity_type = "query"
                entity_id = 0
            else:
                prefixes = [prefix]
        return {
            "type": entity_type,
            "id": entity_id,
            "name": name,
            "category_ids": category_ids,
            "prefixes": prefixes,
        }

    @staticmethod
    def _code_prefix(value, length=5):
        text = str(value or "").strip()
        if text.lower() in {"", "false", "none", "null"}:
            return ""
        normalized = re.sub(r"[^A-Z0-9]+", "", text.upper())
        return normalized[:length] or ""

    @staticmethod
    def _sql_clean_code_expr(expr):
        return (
            "CASE "
            f"WHEN {expr} IS NULL THEN NULL "
            f"WHEN LOWER(BTRIM(COALESCE({expr}, ''))) IN ('', 'false', 'none', 'null') THEN NULL "
            f"ELSE BTRIM({expr}) END"
        )

    @classmethod
    def _sql_code_expr(cls, *exprs):
        return f"COALESCE({', '.join(cls._sql_clean_code_expr(expr) for expr in exprs)})"

    @classmethod
    def _sql_normalized_code_expr(cls, *exprs):
        code_expr = cls._sql_code_expr(*exprs)
        return f"REGEXP_REPLACE(UPPER(COALESCE({code_expr}, '')), '[^A-Z0-9]+', '', 'g')"

    @classmethod
    def _sql_prefix_expr(cls, *exprs, length=5):
        return f"LEFT({cls._sql_normalized_code_expr(*exprs)}, {int(length)})"

    def _anchor_clause(self, entity, query, *, source, scoped=True):
        if entity["type"] == "category" and source == "legacy":
            item_code = "item_code" if scoped else "line.item_code"
            category_id = "category_id" if scoped else "line.product_category_id"
            normalized_prefix = (
                f"LEFT(REGEXP_REPLACE(UPPER(COALESCE({item_code}, '')), '[^A-Z0-9]+', '', 'g'), 5)"
            )
            if entity.get("prefixes"):
                return (
                    f"({category_id} = ANY(%s) OR {normalized_prefix} = ANY(%s))",
                    [entity["category_ids"], entity["prefixes"]],
                )
            return f"{category_id} = ANY(%s)", [entity["category_ids"]]
        if entity["type"] != "query" and entity.get("prefixes") and (
            source == "legacy" or entity["type"] == "legacy_variant"
        ):
            item_code = "item_code" if scoped else "line.item_code"
            if source == "current":
                item_code = "item_code" if scoped else "COALESCE(product.barcode, product.default_code, '')"
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
        company_ids = self._company_ids(filters)
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
                    COALESCE(product.barcode, product.default_code, '') AS item_code,
                    LEFT(REGEXP_REPLACE(UPPER(COALESCE(product.barcode, product.default_code, '')), '[^A-Z0-9]+', '', 'g'), 5) AS prefix5,
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
                SELECT DISTINCT basket_id, product_id, product_name, prefix5
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND NOT ({anchor_sql})
            ),
            companion_totals AS (
                SELECT
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE 'product:' || product_id::text
                    END AS companion_group,
                    MIN(product_id) AS product_id,
                    MIN(product_name) AS product_name,
                    prefix5,
                    COUNT(DISTINCT basket_id) AS co_baskets
                FROM basket_companions
                GROUP BY
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE 'product:' || product_id::text
                    END,
                    prefix5
            ),
            base_totals AS (
                SELECT
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE 'product:' || product_id::text
                    END AS companion_group,
                    prefix5,
                    COUNT(DISTINCT basket_id) AS base_baskets
                FROM scope_lines
                GROUP BY
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE 'product:' || product_id::text
                    END,
                    prefix5
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
                companion.prefix5,
                companion.co_baskets,
                base.base_baskets,
                totals.anchor_baskets,
                totals.companion_baskets,
                totals.all_baskets,
                totals.identified_baskets,
                totals.identified_customers,
                (SELECT product_name FROM anchor_names) AS anchor_name
            FROM companion_totals companion
            JOIN base_totals base ON base.companion_group = companion.companion_group
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
        business_sql, business_params = self._legacy_business_sql("invoice", filters)
        self.env.cr.execute(
            """
            WITH scope_lines AS (
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
                    LEFT(REGEXP_REPLACE(UPPER(COALESCE(line.item_code, '')), '[^A-Z0-9]+', '', 'g'), 5) AS prefix5,
                    line.quantity,
                    line.price_subtotal AS revenue
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  {business_sql}
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
                SELECT DISTINCT basket_id, product_key, product_name, prefix5
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND NOT ({anchor_sql})
            ),
            companion_totals AS (
                SELECT
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE product_key
                    END AS companion_group,
                    MIN(product_key) AS product_key,
                    MIN(product_name) AS product_name,
                    prefix5,
                    COUNT(DISTINCT basket_id) AS co_baskets
                FROM basket_companions
                GROUP BY
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE product_key
                    END,
                    prefix5
            ),
            base_totals AS (
                SELECT
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE product_key
                    END AS companion_group,
                    prefix5,
                    COUNT(DISTINCT basket_id) AS base_baskets
                FROM scope_lines
                GROUP BY
                    CASE
                        WHEN NULLIF(prefix5, '') IS NOT NULL THEN 'prefix:' || prefix5
                        ELSE product_key
                    END,
                    prefix5
            ),
            totals AS (
                SELECT
                    (SELECT COUNT(*) FROM anchor_baskets) AS anchor_baskets,
                    (SELECT COUNT(DISTINCT basket_id) FROM basket_companions) AS companion_baskets,
                    COUNT(DISTINCT basket_id) AS all_baskets,
                    COUNT(DISTINCT basket_id) FILTER (
                        WHERE customer_key IS NOT NULL
                          AND basket_id IN (SELECT basket_id FROM anchor_baskets)
                    ) AS identified_baskets,
                    COUNT(DISTINCT customer_key) FILTER (
                        WHERE customer_key IS NOT NULL
                          AND basket_id IN (SELECT basket_id FROM anchor_baskets)
                    ) AS identified_customers
                FROM scope_lines
            )
            SELECT
                companion.product_key,
                companion.product_name,
                companion.prefix5,
                companion.co_baskets,
                base.base_baskets,
                totals.anchor_baskets,
                totals.companion_baskets,
                totals.all_baskets,
                totals.identified_baskets,
                totals.identified_customers,
                (SELECT product_name FROM anchor_names) AS anchor_name
            FROM companion_totals companion
            JOIN base_totals base ON base.companion_group = companion.companion_group
            CROSS JOIN totals
            ORDER BY companion.co_baskets DESC, companion.product_name
            LIMIT %s
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql, business_sql=business_sql),
            [start, end, *business_params, *audience_params, *anchor_params, *anchor_params, *anchor_params, limit],
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
                  {business_sql}
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
            """.format(anchor_sql=direct_anchor_sql, audience_sql=audience_sql, business_sql=business_sql),
            [start, end, *business_params, *audience_params, *direct_anchor_params],
        )
        customer_columns = [column[0] for column in self.env.cr.description]
        customers = [dict(zip(customer_columns, row)) for row in self.env.cr.fetchall()]

        self.env.cr.execute(
            """
            WITH anchors AS (
                SELECT DISTINCT
                    invoice.id,
                    CONCAT_WS(
                        ' ',
                        CASE
                            WHEN LOWER(BTRIM(COALESCE(invoice.payment_method_summary, '')))
                                 IN ('', 'false', 'none', 'null') THEN NULL
                            ELSE BTRIM(invoice.payment_method_summary)
                        END,
                        CASE
                            WHEN LOWER(BTRIM(COALESCE(invoice.payment_journal_summary, '')))
                                 IN ('', 'false', 'none', 'null') THEN NULL
                            ELSE BTRIM(invoice.payment_journal_summary)
                        END,
                        (
                            SELECT STRING_AGG(
                                DISTINCT COALESCE(
                                    NULLIF(payment.journal_name, ''),
                                    NULLIF(payment.payment_method_name, ''),
                                    NULLIF(payment.name, '')
                                ),
                                ' '
                            )
                            FROM legacy_invoice_payment_link payment
                            WHERE payment.invoice_id = invoice.id
                        )
                    ) AS payment_text
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  {business_sql}
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
            )
            SELECT
                CASE
                    WHEN LOWER(payment_text) LIKE '%%valu%%'
                      OR payment_text LIKE '%%ڤاليو%%'
                      OR payment_text LIKE '%%فاليو%%' THEN 'ValU'
                    WHEN LOWER(payment_text) LIKE '%%souh%%'
                      OR payment_text LIKE '%%سهولة%%' THEN 'Souhoola'
                    WHEN LOWER(payment_text) LIKE '%%tru%%' THEN 'TRU'
                    WHEN LOWER(payment_text) SIMILAR TO '%%(cash|bank|card|visa)%%'
                      OR payment_text SIMILAR TO '%%(نقد|ڤيزا|فيزا|تحويل بنك|شيك)%%' THEN 'Cash / Card'
                    ELSE 'Other'
                END AS payment_group,
                COUNT(*) AS baskets
            FROM anchors
            GROUP BY payment_group
            ORDER BY baskets DESC
            """.format(anchor_sql=direct_anchor_sql, audience_sql=audience_sql, business_sql=business_sql),
            [start, end, *business_params, *audience_params, *direct_anchor_params],
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
            [start, end, self._company_ids(filters), *audience_params, *anchor_params],
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
        business_sql, business_params = self._legacy_business_sql("invoice", filters)
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
                  {business_sql}
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
            """.format(anchor_sql=anchor_sql, audience_sql=audience_sql, business_sql=business_sql),
            [start, end, *business_params, *audience_params, *anchor_params],
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
        legacy_business_sql, legacy_business_params = self._legacy_business_sql("invoice", filters)
        counts = {"current": 0, "legacy": 0}
        company_ids = self._company_ids(filters)
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
                  {business_sql}
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
                """.format(
                    anchor_sql=legacy_anchor_sql,
                    audience_sql=legacy_audience_sql,
                    business_sql=legacy_business_sql,
                ),
                [start, end, *legacy_business_params, *legacy_audience_params, *legacy_anchor_params],
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
                    "status": (
                        "active"
                        if counts[key] and source_used in {key, "unified"}
                        else ("available" if counts[key] else "empty")
                    ),
                    "anchor_baskets": counts[key],
                }
            )
        comparison_available = self._has_table("legacy_product_month_fact")
        coverage.append(
            {
                "key": "history",
                "label": "Product comparison history",
                "status": "available" if comparison_available else "pending",
                "anchor_baskets": 0,
                "note": (
                    "Full January–December 2025 monthly product facts are available and linked to the current catalog by the normalized five-character item-code prefix."
                    if comparison_available
                    else "The migrated monthly product facts are not installed in this database."
                ),
            }
        )
        return {
            "start_date": fields.Date.to_string(start),
            "end_date": fields.Date.to_string(end),
            "sources": coverage,
            "rule": (
                "Historical sales and current operations are combined into one continuous result. "
                "Each invoice remains attributable to its supporting ledger and overlapping identities are joined by the normalized item code."
            ),
        }

    def _comparison_identity_rows(self, prefixes, entity):
        """Prove catalog/fact existence independently from monthly sales activity."""
        prefixes = sorted({prefix for prefix in prefixes if prefix})
        if not prefixes:
            return []
        legacy_code_sql = self._sql_code_expr("source_default_code", "source_barcode")
        legacy_prefix_sql = self._sql_prefix_expr("source_default_code", "source_barcode")
        self.env.cr.execute(
            f"""
            SELECT
                {legacy_prefix_sql} AS prefix5,
                COUNT(DISTINCT source_product_id) AS variant_count,
                MIN(source_name) AS sample_name,
                MIN({legacy_code_sql}) AS sample_code
            FROM legacy_product_month_fact
            WHERE {legacy_prefix_sql} = ANY(%s)
              AND period_month BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
            GROUP BY {legacy_prefix_sql}
            """,
            [prefixes],
        )
        legacy = {
            row[0]: {"count": int(row[1] or 0), "name": row[2] or "", "code": row[3] or ""}
            for row in self.env.cr.fetchall()
        }

        current_code_sql = self._sql_code_expr("product.barcode", "product.default_code")
        current_prefix_sql = self._sql_prefix_expr("product.barcode", "product.default_code")
        self.env.cr.execute(
            f"""
            SELECT
                {current_prefix_sql} AS prefix5,
                COUNT(DISTINCT product.id) AS variant_count,
                COUNT(DISTINCT product.id) FILTER (WHERE product.active AND template.active) AS active_variant_count,
                MIN(product.id) AS sample_id,
                MIN(COALESCE(template.name->>'en_US', template.name->>'en', product.default_code, '')) AS sample_name,
                MIN({current_code_sql}) AS sample_code
            FROM product_product product
            JOIN product_template template ON template.id = product.product_tmpl_id
            WHERE {current_prefix_sql} = ANY(%s)
            GROUP BY {current_prefix_sql}
            """,
            [prefixes],
        )
        current = {
            row[0]: {
                "count": int(row[1] or 0),
                "active_count": int(row[2] or 0),
                "id": int(row[3] or 0),
                "name": row[4] or "",
                "code": row[5] or "",
            }
            for row in self.env.cr.fetchall()
        }

        selected_variant = self.env["product.product"]
        if entity.get("type") == "variant" and entity.get("id"):
            selected_variant = (
                self.env["product.product"].sudo().with_context(active_test=False).browse(entity["id"]).exists()
            )
        rows = []
        for prefix in prefixes:
            legacy_row = legacy.get(prefix, {})
            current_row = current.get(prefix, {})
            has_legacy = bool(legacy_row.get("count"))
            has_current = bool(current_row.get("count"))
            current_is_active = (
                bool(selected_variant.active)
                if selected_variant and prefix in entity.get("prefixes", [])
                else bool(current_row.get("active_count"))
            )
            if has_legacy and has_current:
                state = "matched"
                state_label = (
                    "Matched across history and an archived current product"
                    if not current_is_active
                    else "Matched across historical and current catalogs"
                )
            elif has_legacy:
                state = "legacy_only"
                state_label = "Historical catalog only"
            elif has_current:
                state = "live_only"
                state_label = "Current catalog only"
            else:
                state = "unresolved"
                state_label = "No catalog/fact identity found"
            rows.append(
                {
                    "prefix5": prefix,
                    "state": state,
                    "state_label": state_label,
                    "legacy_exists": has_legacy,
                    "legacy_variant_count": int(legacy_row.get("count") or 0),
                    "legacy_name": legacy_row.get("name") or "",
                    "legacy_item_code": legacy_row.get("code") or "",
                    "current_exists": has_current,
                    "current_variant_count": int(current_row.get("count") or 0),
                    "current_active_variant_count": int(current_row.get("active_count") or 0),
                    "current_catalog_status": "Active" if current_is_active else ("Archived" if has_current else "Missing"),
                    "current_variant_id": (
                        entity.get("id")
                        if entity.get("type") == "variant" and prefix in entity.get("prefixes", [])
                        else int(current_row.get("id") or 0)
                    ),
                    "current_name": (
                        selected_variant.display_name
                        if selected_variant and prefix in entity.get("prefixes", [])
                        else current_row.get("name") or ""
                    ),
                    "current_item_code": (
                        (selected_variant.barcode or selected_variant.default_code or "")
                        if selected_variant and prefix in entity.get("prefixes", [])
                        else current_row.get("code") or ""
                    ),
                }
            )
        return rows

    def _comparison_query_prefixes(self, query, limit=16):
        """Resolve free text to a bounded prefix set before touching the history view.

        `legacy.current.product.history` contains windowed/aggregated live sales. An
        ILIKE predicate against that view forces PostgreSQL to build a broad history
        result before filtering it. The two small identity sources below are cheaper
        and preserve legacy-only as well as current-only catalog candidates.
        """
        limit = max(1, min(int(limit or 16), 24))
        per_source_limit = max(1, (limit + 1) // 2)
        pattern = f"%{(query or '').strip()}%"
        normalized_query = re.sub(r"[^A-Z0-9]+", "", (query or "").upper())
        code_pattern = f"{normalized_query}%" if normalized_query else "#NO_MATCH#"
        legacy_normalized_code_sql = self._sql_normalized_code_expr(
            "source_default_code", "source_barcode"
        )
        legacy_prefix_sql = self._sql_prefix_expr("source_default_code", "source_barcode")
        self.env.cr.execute(
            f"""
            SELECT
                {legacy_prefix_sql} AS prefix5,
                MAX(CASE WHEN {legacy_normalized_code_sql} LIKE %s THEN 1 ELSE 0 END) AS code_match,
                SUM(ABS(COALESCE(legacy_sales_qty, 0.0))) AS sales_weight
            FROM legacy_product_month_fact
            WHERE period_month BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
              AND (
                    source_name ILIKE %s
                 OR source_default_code ILIKE %s
                 OR source_barcode ILIKE %s
                 OR {legacy_normalized_code_sql} LIKE %s
              )
            GROUP BY {legacy_prefix_sql}
            HAVING NULLIF({legacy_prefix_sql}, '') IS NOT NULL
            ORDER BY code_match DESC, sales_weight DESC, prefix5
            LIMIT %s
            """,
            [code_pattern, pattern, pattern, pattern, code_pattern, per_source_limit],
        )
        legacy_prefixes = [row[0] for row in self.env.cr.fetchall() if row[0]]

        current_normalized_code_sql = self._sql_normalized_code_expr(
            "product.barcode", "product.default_code"
        )
        current_prefix_sql = self._sql_prefix_expr("product.barcode", "product.default_code")
        self.env.cr.execute(
            f"""
            SELECT
                {current_prefix_sql} AS prefix5,
                MAX(CASE WHEN {current_normalized_code_sql} LIKE %s THEN 1 ELSE 0 END) AS code_match,
                COUNT(*) FILTER (WHERE product.active AND template.active) AS active_count
            FROM product_product product
            JOIN product_template template ON template.id = product.product_tmpl_id
            WHERE (
                    COALESCE(template.name->>'en_US', template.name->>'en', '') ILIKE %s
                 OR product.default_code ILIKE %s
                 OR product.barcode ILIKE %s
                 OR {current_normalized_code_sql} LIKE %s
            )
            GROUP BY {current_prefix_sql}
            HAVING NULLIF({current_prefix_sql}, '') IS NOT NULL
            ORDER BY code_match DESC, active_count DESC, prefix5
            LIMIT %s
            """,
            [code_pattern, pattern, pattern, pattern, code_pattern, per_source_limit],
        )
        current_prefixes = [row[0] for row in self.env.cr.fetchall() if row[0]]

        # Interleave both sources so a broad current catalog cannot crowd out
        # legacy-only products (and vice versa), while retaining deterministic order.
        prefixes = []
        for index in range(max(len(legacy_prefixes), len(current_prefixes))):
            for candidates in (legacy_prefixes, current_prefixes):
                if index < len(candidates) and candidates[index] not in prefixes:
                    prefixes.append(candidates[index])
                    if len(prefixes) >= limit:
                        return prefixes
        return prefixes

    def _resolve_comparison_prefixes(self, entity, query, limit=16):
        if entity.get("prefixes"):
            return entity["prefixes"], "selected_entity"
        return self._comparison_query_prefixes(query, limit=limit), "bounded_query"

    def _comparison_legacy_metric_rows(self, prefixes):
        """Aggregate only the requested legacy prefixes from the physical fact table."""
        prefixes = sorted({prefix for prefix in prefixes if prefix})
        if not prefixes:
            return []
        prefix_sql = self._sql_prefix_expr("fact.source_default_code", "fact.source_barcode")
        self.env.cr.execute(
            f"""
            SELECT
                'legacy'::text AS source_system,
                EXTRACT(MONTH FROM fact.period_month)::integer AS month_number,
                MIN(fact.period_month) AS period_month,
                COALESCE(SUM(fact.legacy_sales_qty), 0.0) AS sales_qty,
                COALESCE(SUM(fact.legacy_sales_amount), 0.0) AS sales_amount,
                COALESCE(SUM(fact.legacy_return_qty), 0.0) AS return_qty,
                COALESCE(SUM(fact.legacy_return_amount), 0.0) AS return_amount,
                COALESCE(SUM(fact.legacy_discount_amount), 0.0) AS discount_amount,
                COALESCE(SUM(fact.legacy_gross_sales_amount), 0.0) AS gross_sales_amount,
                COUNT(DISTINCT NULLIF({prefix_sql}, '')) AS prefix_count,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT {prefix_sql}), NULL) AS observed_prefixes
            FROM legacy_product_month_fact fact
            WHERE {prefix_sql} = ANY(%s)
              AND fact.period_month BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
            GROUP BY EXTRACT(MONTH FROM fact.period_month)::integer
            ORDER BY month_number
            """,
            [prefixes],
        )
        columns = [column[0] for column in self.env.cr.description]
        return [dict(zip(columns, row)) for row in self.env.cr.fetchall()]

    def _comparison_current_product_ids(self, prefixes, entity):
        """Resolve prefix identity to product ids before reading invoice activity.

        A selected variant remains exact on the Odoo 18 side. Product and category
        selections also retain their catalog grain; only free-text and legacy-only
        selections expand to every current variant carrying a resolved prefix.
        """
        prefixes = sorted({prefix for prefix in prefixes if prefix})
        if not prefixes:
            return []
        prefix_sql = self._sql_prefix_expr("product.barcode", "product.default_code")
        scope_sql = ""
        scope_params = []
        if entity.get("type") == "variant" and entity.get("id"):
            scope_sql = "AND product.id = %s"
            scope_params = [entity["id"]]
        elif entity.get("type") == "product" and entity.get("id"):
            scope_sql = "AND product.product_tmpl_id = %s"
            scope_params = [entity["id"]]
        elif entity.get("type") == "category" and entity.get("category_ids"):
            scope_sql = "AND template.categ_id = ANY(%s)"
            scope_params = [entity["category_ids"]]
        self.env.cr.execute(
            f"""
            SELECT product.id
            FROM product_product product
            JOIN product_template template ON template.id = product.product_tmpl_id
            WHERE {prefix_sql} = ANY(%s)
              {scope_sql}
            ORDER BY product.id
            """,
            [prefixes, *scope_params],
        )
        return [int(row[0]) for row in self.env.cr.fetchall()]

    def _comparison_current_metric_rows(self, prefixes, entity, filters):
        """Aggregate current metrics from indexed posted customer invoice lines.

        ``account.invoice.report`` is a virtual model whose custom table query also
        computes stock-valuation and margin fields that this comparison never uses.
        Expanding that full query made a bounded 14-variant comparison take around
        a minute on stage.  These two measures come directly from the same posted
        invoice-line evidence: signed UoM-normalized quantity and signed untaxed
        company-currency balance.
        """
        product_ids = self._comparison_current_product_ids(prefixes, entity)
        metadata = {
            "source": "account.move.line",
            "product_count": len(product_ids),
            "available": True,
            "quantity_metric": "Signed net invoiced quantity",
            "amount_metric": "Signed untaxed company-currency invoice balance",
        }
        if not product_ids:
            return [], metadata
        company_ids = self._company_ids(filters)
        current_prefix_sql = self._sql_prefix_expr("product.barcode", "product.default_code")
        self.env.cr.execute(
            f"""
            SELECT
                'current'::text AS source_system,
                EXTRACT(MONTH FROM move.invoice_date)::integer AS month_number,
                MIN(move.invoice_date) AS period_month,
                COALESCE(SUM(
                    COALESCE(line.quantity, 0.0)
                    / NULLIF(
                        COALESCE(line_uom.factor, 1.0)
                        / COALESCE(template_uom.factor, 1.0),
                        0.0
                    )
                    * CASE WHEN move.move_type = 'out_refund' THEN -1.0 ELSE 1.0 END
                ), 0.0) AS sales_qty,
                COALESCE(SUM(-COALESCE(line.balance, 0.0)), 0.0) AS sales_amount,
                COALESCE(SUM(
                    CASE WHEN move.move_type = 'out_refund' THEN
                        COALESCE(line.quantity, 0.0)
                        / NULLIF(
                            COALESCE(line_uom.factor, 1.0)
                            / COALESCE(template_uom.factor, 1.0),
                            0.0
                        )
                        * -1.0
                    ELSE 0.0 END
                ), 0.0) AS return_qty,
                COALESCE(SUM(
                    CASE WHEN move.move_type = 'out_refund' THEN -COALESCE(line.balance, 0.0) ELSE 0.0 END
                ), 0.0) AS return_amount,
                0.0::double precision AS discount_amount,
                0.0::double precision AS gross_sales_amount,
                COUNT(DISTINCT NULLIF({current_prefix_sql}, '')) AS prefix_count,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT {current_prefix_sql}), NULL) AS observed_prefixes
            FROM account_move_line line
            JOIN account_move move ON move.id = line.move_id
            JOIN product_product product ON product.id = line.product_id
            JOIN product_template template ON template.id = product.product_tmpl_id
            LEFT JOIN uom_uom line_uom ON line_uom.id = line.product_uom_id
            LEFT JOIN uom_uom template_uom ON template_uom.id = template.uom_id
            WHERE line.product_id = ANY(%s)
              AND line.company_id = ANY(%s)
              AND move.state = 'posted'
              AND move.move_type IN ('out_invoice', 'out_refund')
              AND move.invoice_date BETWEEN DATE '2026-01-01' AND CURRENT_DATE
              AND line.display_type = 'product'
            GROUP BY EXTRACT(MONTH FROM move.invoice_date)::integer
            ORDER BY month_number
            """,
            [product_ids, company_ids],
        )
        columns = [column[0] for column in self.env.cr.description]
        return [dict(zip(columns, row)) for row in self.env.cr.fetchall()], metadata

    @api.model
    def get_legacy_comparison(self, query, entity=None, filters=None):
        """Compare the full legacy fact history with live Odoo using Tradeline's prefix-5 item identity."""
        self._ensure_access()
        query = (query or "").strip()
        entity = self._normalize_entity(entity, query)
        filters = self._normalize_filters(filters)
        if entity["type"] == "query" and len(query) < 2:
            raise UserError("Choose a product or enter at least two search characters.")
        if not self._has_table("legacy_product_month_fact"):
            return {
                "available": False,
                "match_rule": "prefix5_only",
                "rule_label": "First 5 normalized item-code characters",
                "note": "The migrated monthly product fact table is not installed in this database.",
                "months": [],
            }

        resolved_prefixes, resolution_mode = self._resolve_comparison_prefixes(entity, query, limit=16)
        legacy_rows = self._comparison_legacy_metric_rows(resolved_prefixes)
        current_rows, current_metric_metadata = self._comparison_current_metric_rows(
            resolved_prefixes, entity, filters
        )
        rows = [*legacy_rows, *current_rows]

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
                # Odoo XML-RPC cannot marshal ``None``; False is its standard
                # transport-safe representation for an unavailable scalar.
                return False
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
        selected_prefixes = resolved_prefixes
        identity_rows = self._comparison_identity_rows(selected_prefixes, entity)
        shared_prefixes = [row["prefix5"] for row in identity_rows if row["state"] == "matched"]
        legacy_only_prefixes = [row["prefix5"] for row in identity_rows if row["state"] == "legacy_only"]
        current_only_prefixes = [row["prefix5"] for row in identity_rows if row["state"] == "live_only"]
        identity_states = {row["state"] for row in identity_rows}
        identity_state = next(iter(identity_states)) if len(identity_states) == 1 else ("mixed" if identity_states else "unresolved")
        identity_labels = {
            "matched": "Matched variant identity",
            "legacy_only": "Historical-only variant identity",
            "live_only": "Current-only variant identity",
            "mixed": "Mixed identity coverage",
            "unresolved": "Identity unresolved",
        }
        return {
            "available": True,
            "match_rule": "prefix5_only",
            "rule_label": "First 5 normalized item-code characters",
            "normalization": (
                "Use the first available item code, remove non-alphanumeric characters, uppercase it, then take the first five characters. "
                + (
                    "This free-text scope uses at most 16 ranked prefixes resolved across historical facts and the current catalog; choose a variant for an exact one-prefix comparison."
                    if resolution_mode == "bounded_query"
                    else "The selected entity supplies the exact comparison prefix set."
                )
            ),
            "scope_type": entity["type"],
            "scope_name": entity["name"] if entity["type"] != "query" else query,
            "prefixes": selected_prefixes,
            "prefix_count": len(selected_prefixes),
            "prefix_resolution_mode": resolution_mode,
            "prefix_resolution_limit": 16,
            "shared_prefix_count": len(shared_prefixes),
            "legacy_only_prefix_count": len(legacy_only_prefixes),
            "current_only_prefix_count": len(current_only_prefixes),
            "identity_state": identity_state,
            "identity_state_label": identity_labels[identity_state],
            "identity_rows": identity_rows,
            "company_scope": {
                "id": filters["operating_company_id"],
                "name": filters["operating_company_name"] or "All operating companies",
                "current_activity_scoped": bool(filters["operating_company_id"]),
                "legacy_company_dimension_available": False,
                "note": (
                    "Current activity is filtered to this business. Historical monthly product facts do not carry an operating-company dimension, so historical totals remain all-business."
                    if filters["operating_company_id"]
                    else "Current activity includes every allowed business. Historical monthly facts are all-business."
                ),
            },
            "metric_provenance": {
                "legacy_source": "legacy.product.month.fact",
                "legacy_quantity_metric": "legacy_sales_qty",
                "legacy_amount_metric": "legacy_sales_amount",
                "current_source": current_metric_metadata["source"],
                "current_source_available": current_metric_metadata["available"],
                "current_product_count": current_metric_metadata["product_count"],
                "current_quantity_metric": current_metric_metadata["quantity_metric"],
                "current_amount_metric": current_metric_metadata["amount_metric"],
                "limitations": [
                    "Odoo 18 amount is the signed untaxed company-currency balance from posted customer invoice lines; it excludes tax and is not the tax-inclusive invoice total.",
                    "Odoo 18 discount and gross-sales components are not used by this comparison UI; quantity and amount/deltas are authoritative.",
                    "Odoo 12 monthly product facts have no operating-company dimension.",
                ],
            },
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
                "basket_level": "Full 2025 invoice-level basket and customer evidence",
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
                f"{category_name} · {variant_count} variant{'s' if variant_count != 1 else ''}"
                + (" · Archived in current catalog" if not template.active else ""),
            )

        variant_quota = max(3, limit - product_quota - 2)
        for variant in variants[:variant_quota]:
            item_code = variant.barcode or variant.default_code or ""
            normalized_code = re.sub(r"[^A-Z0-9]+", "", item_code.upper())
            match_hint = "Matched by item code" if normalized_query and normalized_query in normalized_code else ""
            append_item(
                variant,
                "variant",
                f"{variant.categ_id.display_name or 'Uncategorized'}"
                + (f" · {item_code}" if item_code else "")
                + (" · Archived in current catalog" if not variant.active else ""),
                item_code,
                match_hint,
            )

        if len(output) < limit and self._has_table("legacy_product_month_fact"):
            legacy_code_sql = self._sql_code_expr("source_default_code", "source_barcode")
            legacy_prefix_sql = self._sql_prefix_expr("source_default_code", "source_barcode")
            self.env.cr.execute(
                f"""
                SELECT
                    {legacy_prefix_sql} AS prefix5,
                    MIN(COALESCE(NULLIF(source_name, ''), {legacy_code_sql})) AS label,
                    MIN({legacy_code_sql}) AS item_code,
                    COUNT(DISTINCT source_product_id) AS variant_count
                FROM legacy_product_month_fact
                WHERE period_month BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
                  AND (
                        source_name ILIKE %s
                     OR source_default_code ILIKE %s
                     OR source_barcode ILIKE %s
                  )
                GROUP BY {legacy_prefix_sql}
                HAVING NULLIF({legacy_prefix_sql}, '') IS NOT NULL
                ORDER BY COUNT(*) DESC, label
                LIMIT %s
                """,
                [f"%{query}%", f"%{query}%", f"%{query}%", max(2, limit // 3)],
            )
            for prefix5, label, item_code, variant_count in self.env.cr.fetchall():
                key = f"legacy-variant:{prefix5}"
                if not label or key in seen or len(output) >= limit:
                    continue
                seen.add(key)
                output.append(
                    {
                        "key": key,
                        "id": 0,
                        "name": label,
                        "type": "legacy_variant",
                        "source": "legacy",
                        "subtitle": f"Historical product facts · {item_code or 'no item code'} · {variant_count} source variant{'s' if variant_count != 1 else ''}",
                        "item_code": item_code or "",
                        "prefix5": prefix5,
                        "match_hint": "Compared by normalized prefix-5",
                    }
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
                        "subtitle": "Historical basket text match",
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

    @staticmethod
    def _catalog_brand(product_name):
        """Return a human brand label when the catalog has no dedicated brand field.

        Tradeline's current catalog carries category, vendor and variant attributes,
        but it does not carry a brand model.  Product names do carry a stable brand
        token.  Keep this derivation deliberately conservative so it remains a
        navigation aid and never becomes a cross-system identity key.
        """
        name = re.sub(r"^\s*\[[^]]+\]\s*", "", (product_name or "").strip())
        if not name:
            return "Other"
        if re.search(r"\b(iphone|ipad|macbook|imac|airpods|apple watch|mac mini)\b", name, re.I):
            return "Apple"
        token = re.split(r"[\s\-/|]+", name, maxsplit=1)[0].strip(".,()[]")
        if not token or token.isdigit() or len(token) < 2:
            return "Other"
        known = {
            "apple": "Apple",
            "belkin": "Belkin",
            "anker": "Anker",
            "uniq": "Uniq",
            "laut": "Laut",
            "beats": "Beats",
            "samsung": "Samsung",
            "huawei": "Huawei",
            "honor": "Honor",
            "xiaomi": "Xiaomi",
            "oppo": "OPPO",
            "jbl": "JBL",
        }
        return known.get(token.lower(), token[:1].upper() + token[1:])

    def _catalog_rows(self):
        """Read the live catalog once for a complete cascading selector response."""
        self.env.cr.execute(
            """
            SELECT
                product.id AS variant_id,
                template.id AS product_id,
                COALESCE(
                    NULLIF(template.name->>'en_US', ''),
                    NULLIF(template.name->>'en', ''),
                    NULLIF(product.default_code, ''),
                    'Product ' || product.id::text
                ) AS product_name,
                category.id AS category_id,
                REGEXP_REPLACE(COALESCE(NULLIF(category.complete_name, ''), 'Uncategorized'), '^.*/[[:space:]]*', '') AS category_name,
                product.vendor_id,
                COALESCE(vendor.name, 'Unassigned vendor') AS vendor_name,
                COALESCE(product.barcode, product.default_code, '') AS item_code
            FROM product_product product
            JOIN product_template template ON template.id = product.product_tmpl_id
            LEFT JOIN product_category category ON category.id = template.categ_id
            LEFT JOIN res_partner vendor ON vendor.id = product.vendor_id
            WHERE COALESCE(product.active, TRUE)
              AND COALESCE(template.active, TRUE)
              AND COALESCE(template.sale_ok, TRUE)
            ORDER BY product_name, product.id
            """
        )
        columns = [column[0] for column in self.env.cr.description]
        rows = [dict(zip(columns, values)) for values in self.env.cr.fetchall()]
        for row in rows:
            row["brand"] = self._catalog_brand(row["product_name"])
            row["prefix5"] = self._code_prefix(row["item_code"])
        return rows

    @api.model
    def get_catalog_options(self, selection=None):
        """Return a complete Brand → Vendor → Category → Product → Variant path.

        The visible path is entirely human-readable.  Exact variants include a
        transport-safe prefix only so the service can join the historical and
        current ledgers after selection; the UI never needs to display it.
        """
        self._ensure_access()
        selection = dict(selection or {})
        try:
            variant_id = int(selection.get("variant_id") or 0)
        except (TypeError, ValueError):
            variant_id = 0
        try:
            product_id = int(selection.get("product_id") or 0)
        except (TypeError, ValueError):
            product_id = 0
        try:
            category_id = int(selection.get("category_id") or 0)
        except (TypeError, ValueError):
            category_id = 0
        try:
            vendor_id = int(selection.get("vendor_id") or 0)
        except (TypeError, ValueError):
            vendor_id = 0
        brand = (selection.get("brand") or "").strip()

        rows = self._catalog_rows()
        by_variant = {row["variant_id"]: row for row in rows}
        if variant_id and variant_id in by_variant:
            selected = by_variant[variant_id]
            brand = selected["brand"]
            vendor_id = int(selected["vendor_id"] or 0)
            category_id = int(selected["category_id"] or 0)
            product_id = int(selected["product_id"] or 0)

        def option_rows(items, key, label, *, count_key="variant_id"):
            grouped = {}
            for item in items:
                option_key = item.get(key)
                if option_key in (None, ""):
                    continue
                current = grouped.setdefault(
                    option_key,
                    {"id": option_key, "name": item.get(label) or str(option_key), "count": 0},
                )
                current["count"] += 1 if item.get(count_key) else 0
            return sorted(grouped.values(), key=lambda item: (item["name"].lower(), str(item["id"])))

        brands = []
        for option in option_rows(rows, "brand", "brand"):
            brands.append({"key": option["id"], "name": option["name"], "count": option["count"]})

        brand_rows = [row for row in rows if not brand or row["brand"].lower() == brand.lower()]
        vendors = option_rows(brand_rows, "vendor_id", "vendor_name")
        vendor_rows = [row for row in brand_rows if not vendor_id or int(row["vendor_id"] or 0) == vendor_id]
        categories = option_rows(vendor_rows, "category_id", "category_name")
        category_rows = [row for row in vendor_rows if not category_id or int(row["category_id"] or 0) == category_id]
        products = option_rows(category_rows, "product_id", "product_name")
        product_rows = [row for row in category_rows if not product_id or int(row["product_id"] or 0) == product_id]

        variants = []
        if product_id:
            variant_records = self.env["product.product"].sudo().browse(
                [row["variant_id"] for row in product_rows]
            ).exists()
            display_names = {record.id: record.display_name for record in variant_records}
            prefixes = sorted({row["prefix5"] for row in product_rows if row["prefix5"]})
            historical_prefixes = set()
            if prefixes and self._has_table("legacy_product_month_fact"):
                prefix_sql = self._sql_prefix_expr("source_default_code", "source_barcode")
                self.env.cr.execute(
                    f"""
                    SELECT DISTINCT {prefix_sql}
                    FROM legacy_product_month_fact
                    WHERE {prefix_sql} = ANY(%s)
                    """,
                    [prefixes],
                )
                historical_prefixes = {row[0] for row in self.env.cr.fetchall() if row[0]}
            for row in product_rows:
                prefix = row["prefix5"]
                variants.append(
                    {
                        "id": row["variant_id"],
                        "name": display_names.get(row["variant_id"]) or row["product_name"],
                        "item_code": row["item_code"],
                        "prefix5": prefix,
                        "type": "variant",
                        "source": "unified" if prefix in historical_prefixes else "current",
                        "coverage_label": "Historical + current" if prefix in historical_prefixes else "Current catalog",
                    }
                )
            variants.sort(key=lambda item: item["name"].lower())

        selected_variant = next((item for item in variants if item["id"] == variant_id), None)
        return self._transport_safe(
            {
                "selection": {
                    "brand": brand,
                    "vendor_id": vendor_id,
                    "category_id": category_id,
                    "product_id": product_id,
                    "variant_id": variant_id,
                },
                "brands": brands,
                "vendors": vendors,
                "categories": categories,
                "products": products,
                "variants": variants,
                "selected_variant": selected_variant,
                "join_rule": "normalized_item_prefix_5",
            }
        )

    def _available_period(self, entity, query, filters=None):
        """Find the real first and last supporting invoice for the selected scope."""
        filters = self._normalize_filters(filters)
        current_sql, current_params = self._anchor_clause(entity, query, source="current", scoped=False)
        current_audience_sql, current_audience_params = self._audience_sql("move", filters)
        company_ids = self._company_ids(filters)
        self.env.cr.execute(
            """
            SELECT MIN(move.invoice_date), MAX(move.invoice_date), COUNT(DISTINCT move.id)
            FROM account_move_line line
            JOIN account_move move ON move.id = line.move_id
            JOIN product_product product ON product.id = line.product_id
            JOIN product_template template ON template.id = product.product_tmpl_id
            WHERE move.state = 'posted'
              AND move.move_type IN ('out_invoice', 'out_receipt')
              AND move.invoice_date >= DATE '2025-01-01'
              AND move.company_id = ANY(%s)
              {audience_sql}
              AND line.quantity > 0
              AND {anchor_sql}
            """.format(anchor_sql=current_sql, audience_sql=current_audience_sql),
            [company_ids, *current_audience_params, *current_params],
        )
        current_start, current_end, current_count = self.env.cr.fetchone()
        legacy_start = legacy_end = None
        legacy_count = 0
        if self._has_table("legacy_invoice_line"):
            legacy_sql, legacy_params = self._anchor_clause(entity, query, source="legacy", scoped=False)
            legacy_audience_sql, legacy_audience_params = self._audience_sql("invoice", filters, source="legacy")
            business_sql, business_params = self._legacy_business_sql("invoice", filters)
            self.env.cr.execute(
                """
                SELECT MIN(invoice.invoice_date), MAX(invoice.invoice_date), COUNT(DISTINCT invoice.id)
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                WHERE invoice.invoice_date >= DATE '2025-01-01'
                  {business_sql}
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
                """.format(
                    anchor_sql=legacy_sql,
                    audience_sql=legacy_audience_sql,
                    business_sql=business_sql,
                ),
                [*business_params, *legacy_audience_params, *legacy_params],
            )
            legacy_start, legacy_end, legacy_count = self.env.cr.fetchone()
        starts = [value for value in (legacy_start, current_start) if value]
        ends = [value for value in (legacy_end, current_end) if value]
        start = min(starts) if starts else date(2025, 1, 1)
        end = max(ends) if ends else fields.Date.today()
        return {
            "start_date": fields.Date.to_string(start),
            "end_date": fields.Date.to_string(end),
            "label": f"{start.strftime('%b %Y')} — {end.strftime('%b %Y')}",
            "current_baskets": int(current_count or 0),
            "historical_baskets": int(legacy_count or 0),
        }

    @api.model
    def get_available_period(self, query, entity=None, filters=None):
        self._ensure_access()
        query = (query or "").strip()
        normalized = self._normalize_entity(entity, query)
        return self._transport_safe(self._available_period(normalized, query, filters))

    @api.model
    def get_filter_options(self):
        """Small, human-readable filter dictionary for the command bar."""
        self._ensure_access()
        operating_companies = self.env.user.company_ids.sorted("name")
        if not operating_companies:
            operating_companies = self.env.company
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
            "operating_companies": [
                {"id": company.id, "name": company.name} for company in operating_companies
            ],
            "default_operating_company_id": 0,
        }

    @staticmethod
    def _customer_identity(customer):
        if customer.get("partner_id"):
            return f"partner:{int(customer['partner_id'])}"
        email = (customer.get("email") or "").strip().lower()
        if email:
            return f"email:{email}"
        mobile = re.sub(r"[^0-9+]", "", customer.get("mobile") or "")
        if mobile:
            return f"mobile:{mobile}"
        return f"name:{(customer.get('name') or 'unknown').strip().lower()}"

    def _merge_customers(self, source_rows):
        merged = {}
        for source, rows in source_rows:
            for row in rows:
                key = self._customer_identity(row)
                current = merged.setdefault(
                    key,
                    {
                        "customer_key": key,
                        "partner_id": row.get("partner_id") or 0,
                        "name": row.get("name") or "Customer",
                        "email": row.get("email") or "",
                        "mobile": row.get("mobile") or "",
                        "is_company": bool(row.get("is_company")),
                        "company_name": row.get("company_name") or "",
                        "baskets": 0,
                        "revenue": 0.0,
                        "last_purchase": None,
                        "evidence_sources": [],
                    },
                )
                current["partner_id"] = current["partner_id"] or row.get("partner_id") or 0
                current["email"] = current["email"] or row.get("email") or ""
                current["mobile"] = current["mobile"] or row.get("mobile") or ""
                current["company_name"] = current["company_name"] or row.get("company_name") or ""
                current["is_company"] = current["is_company"] or bool(row.get("is_company"))
                current["baskets"] += int(row.get("baskets") or 0)
                current["revenue"] += float(row.get("revenue") or 0.0)
                last_purchase = row.get("last_purchase")
                if last_purchase and (not current["last_purchase"] or last_purchase > current["last_purchase"]):
                    current["last_purchase"] = last_purchase
                if source not in current["evidence_sources"]:
                    current["evidence_sources"].append(source)
        return sorted(merged.values(), key=lambda row: (-row["baskets"], -row["revenue"], row["name"]))[:100]

    def _deduplicated_customer_count(self, query, start, end, entity, filters, source_used, source_periods=None):
        """Count customers once after resolving identities across both ledgers."""
        sources = ("current", "legacy") if source_used == "unified" else (source_used,)
        identities = set()
        for source in sources:
            source_start, source_end = (source_periods or {}).get(source, (start, end))
            for customer in self._query_ownership_customers(
                source, query, source_start, source_end, entity, filters
            ):
                identities.add(self._customer_identity(customer))
        return len(identities)

    def _product_identity_summary(self, entity):
        prefixes = entity.get("prefixes") or []
        if not prefixes or not self._has_table("legacy_product_month_fact"):
            return {
                "identity_precision": "current_only",
                "identity_label": "Current catalog identity",
                "identity_note": "The selected current-catalog grain is used exactly; no historical comparison identity was resolved.",
                "legacy_variant_count": 0,
                "current_variant_count": 1 if entity.get("type") == "variant" else 0,
            }
        rows = self._comparison_identity_rows(prefixes, entity)
        legacy_count = sum(int(row.get("legacy_variant_count") or 0) for row in rows)
        current_count = sum(int(row.get("current_variant_count") or 0) for row in rows)
        ambiguous = any(int(row.get("legacy_variant_count") or 0) > 1 for row in rows)
        if ambiguous:
            prefix_label = ", ".join(prefixes)
            return {
                "identity_precision": "prefix_family",
                "identity_label": "Exact current variant + historical prefix family",
                "identity_note": (
                    f"Current activity uses the exact selected variant. Historical activity combines {legacy_count} "
                    f"legacy item codes sharing the approved five-character prefix {prefix_label}; evidence remains separately traceable."
                ),
                "legacy_variant_count": legacy_count,
                "current_variant_count": current_count,
            }
        return {
            "identity_precision": "exact",
            "identity_label": "One-to-one cross-version identity",
            "identity_note": "The selected current variant resolves to one historical item-code identity using the approved five-character prefix.",
            "legacy_variant_count": legacy_count,
            "current_variant_count": current_count,
        }

    @staticmethod
    def _merge_dimension_rows(groups, *, value_fields):
        merged = {}
        for rows in groups:
            for row in rows or []:
                key = row.get("period") or row.get("name") or "Unknown"
                if key not in merged:
                    merged[key] = dict(row)
                    continue
                current = merged[key]
                for field_name in value_fields:
                    current[field_name] = float(current.get(field_name) or 0) + float(row.get(field_name) or 0)
        output = list(merged.values())
        if output and output[0].get("period"):
            output.sort(key=lambda row: row.get("period") or "")
        else:
            output.sort(key=lambda row: (-float(row.get("baskets") or 0), row.get("name") or ""))
        return output

    def _merge_dimensions(self, current, legacy):
        current = current or {}
        legacy = legacy or {}
        current_summary = current.get("scope_summary") or {}
        legacy_summary = legacy.get("scope_summary") or {}
        return {
            "scope_summary": {
                key: int(current_summary.get(key) or 0) + int(legacy_summary.get(key) or 0)
                for key in ("baskets", "identified_baskets", "identified_customers")
            },
            "trend": self._merge_dimension_rows(
                [current.get("trend"), legacy.get("trend")], value_fields=("baskets", "revenue")
            ),
            "store_mix": self._merge_dimension_rows(
                [current.get("store_mix"), legacy.get("store_mix")], value_fields=("baskets", "revenue")
            )[:8],
            "salesperson_mix": self._merge_dimension_rows(
                [current.get("salesperson_mix"), legacy.get("salesperson_mix")], value_fields=("baskets", "revenue")
            )[:8],
            "discount_mix": self._merge_dimension_rows(
                [current.get("discount_mix"), legacy.get("discount_mix")], value_fields=("baskets",)
            ),
            "channel_mix": self._merge_dimension_rows(
                [current.get("channel_mix"), legacy.get("channel_mix")], value_fields=("baskets",)
            )[:8],
        }

    def _merge_source_results(self, current_result, legacy_result, limit):
        current_companions, current_customers, current_payments, current_dimensions = current_result
        legacy_companions, legacy_customers, legacy_payments, legacy_dimensions = legacy_result
        merged_companions = {}
        source_totals = {}
        for source, rows in (("current", current_companions), ("legacy", legacy_companions)):
            if rows:
                source_totals[source] = {
                    "anchor_baskets": int(rows[0].get("anchor_baskets") or 0),
                    "companion_baskets": int(rows[0].get("companion_baskets") or 0),
                    "all_baskets": int(rows[0].get("all_baskets") or 0),
                    "identified_baskets": int(rows[0].get("identified_baskets") or 0),
                    "identified_customers": int(rows[0].get("identified_customers") or 0),
                }
            for row in rows:
                prefix = self._code_prefix(row.get("prefix5"))
                name_key = re.sub(r"\W+", "", (row.get("product_name") or "").lower())
                key = f"prefix:{prefix}" if prefix else f"name:{name_key}"
                current = merged_companions.setdefault(
                    key,
                    {
                        "product_key": key,
                        "product_name": row.get("product_name") or "Product",
                        "prefix5": prefix,
                        "co_baskets": 0,
                        "base_baskets": 0,
                        "evidence_sources": [],
                        "source_keys": {},
                    },
                )
                current["co_baskets"] += int(row.get("co_baskets") or 0)
                current["base_baskets"] += int(row.get("base_baskets") or 0)
                current["source_keys"][source] = row.get("product_key") or ""
                if source not in current["evidence_sources"]:
                    current["evidence_sources"].append(source)

        totals = {
            key: sum(source.get(key, 0) for source in source_totals.values())
            for key in ("anchor_baskets", "companion_baskets", "all_baskets", "identified_baskets", "identified_customers")
        }
        companions = sorted(
            merged_companions.values(),
            key=lambda row: (-row["co_baskets"], row["product_name"].lower()),
        )[: int(limit or 20)]
        for row in companions:
            row.update(totals)

        payments = defaultdict(int)
        for rows in (current_payments, legacy_payments):
            for row in rows:
                payments[row.get("name") or "Other"] += int(row.get("baskets") or 0)
        payment_rows = [
            {"name": name, "baskets": baskets}
            for name, baskets in sorted(payments.items(), key=lambda item: (-item[1], item[0]))
        ]
        dimensions = self._merge_dimensions(current_dimensions, legacy_dimensions)
        dimensions["scope_summary"].update(
            {
                "baskets": totals["anchor_baskets"],
                "companion_baskets": totals["companion_baskets"],
                "all_baskets": totals["all_baskets"],
            }
        )
        customers = self._merge_customers(
            [("current", current_customers), ("legacy", legacy_customers)]
        )
        return companions, customers, payment_rows, dimensions

    def _ownership_entity(self, entity):
        if entity.get("type") == "category":
            return entity
        category = self.env["product.category"]
        if entity.get("type") == "variant" and entity.get("id"):
            category = self.env["product.product"].sudo().browse(entity["id"]).exists().categ_id
        elif entity.get("type") == "product" and entity.get("id"):
            category = self.env["product.template"].sudo().browse(entity["id"]).exists().categ_id
        elif entity.get("prefixes"):
            prefix_sql = self._sql_prefix_expr("product.barcode", "product.default_code")
            self.env.cr.execute(
                f"""
                SELECT template.categ_id
                FROM product_product product
                JOIN product_template template ON template.id = product.product_tmpl_id
                WHERE {prefix_sql} = ANY(%s)
                ORDER BY product.active DESC, product.id
                LIMIT 1
                """,
                [entity["prefixes"]],
            )
            row = self.env.cr.fetchone()
            category = self.env["product.category"].sudo().browse(row[0]).exists() if row else category
        if category:
            return self._normalize_entity({"type": "category", "id": category.id}, category.display_name)
        return entity

    @staticmethod
    def _iphone_generation(product_name):
        match = re.search(r"\biphone\s*(\d{1,2})\b", product_name or "", re.I)
        return int(match.group(1)) if match else 0

    def _query_ownership_source(self, source, query, start, end, entity, filters=None):
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source=source, scoped=False)
        if source == "current":
            audience_sql, audience_params = self._audience_sql("move", filters)
            self.env.cr.execute(
                """
                SELECT
                    partner.id AS partner_id,
                    partner.name,
                    partner.email,
                    partner.mobile,
                    COALESCE(commercial.is_company, FALSE) AS is_company,
                    CASE WHEN commercial.is_company THEN commercial.name ELSE NULL END AS company_name,
                    product.id AS product_id,
                    COALESCE(template.name->>'en_US', template.name->>'en', product.default_code, 'Product') AS product_name,
                    LEFT(REGEXP_REPLACE(UPPER(COALESCE(product.barcode, product.default_code, '')), '[^A-Z0-9]+', '', 'g'), 5) AS prefix5,
                    COUNT(DISTINCT move.id) AS baskets,
                    SUM(line.quantity) AS units,
                    SUM(line.price_subtotal) AS revenue,
                    MIN(move.invoice_date) AS first_purchase,
                    MAX(move.invoice_date) AS last_purchase
                FROM account_move_line line
                JOIN account_move move ON move.id = line.move_id
                JOIN product_product product ON product.id = line.product_id
                JOIN product_template template ON template.id = product.product_tmpl_id
                JOIN res_partner partner ON partner.id = move.partner_id
                JOIN res_partner commercial ON commercial.id = partner.commercial_partner_id
                WHERE move.state = 'posted'
                  AND move.move_type IN ('out_invoice', 'out_receipt')
                  AND move.invoice_date BETWEEN %s AND %s
                  AND move.company_id = ANY(%s)
                  {audience_sql}
                  AND line.quantity > 0
                  AND {anchor_sql}
                GROUP BY partner.id, partner.name, partner.email, partner.mobile,
                         commercial.is_company, commercial.name, product.id, product_name, prefix5
                ORDER BY last_purchase DESC, revenue DESC
                LIMIT 3000
                """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
                [start, end, self._company_ids(filters), *audience_params, *anchor_params],
            )
        else:
            if not self._has_table("legacy_invoice_line"):
                return []
            audience_sql, audience_params = self._audience_sql("invoice", filters, source="legacy")
            business_sql, business_params = self._legacy_business_sql("invoice", filters)
            self.env.cr.execute(
                """
                SELECT
                    partner.id AS partner_id,
                    COALESCE(partner.name, NULLIF(invoice.source_partner_name, ''), 'Historical customer') AS name,
                    partner.email,
                    COALESCE(partner.mobile, NULLIF(invoice.source_partner_mobile, '')) AS mobile,
                    (COALESCE(commercial.is_company, FALSE) OR LOWER(COALESCE(invoice.source_partner_type, '')) = 'company') AS is_company,
                    CASE
                        WHEN COALESCE(commercial.is_company, FALSE) THEN commercial.name
                        WHEN LOWER(COALESCE(invoice.source_partner_type, '')) = 'company' THEN invoice.source_partner_name
                    END AS company_name,
                    line.product_id,
                    COALESCE(NULLIF(line.product_name, ''), NULLIF(line.name, ''), NULLIF(line.item_code, ''), 'Product') AS product_name,
                    LEFT(REGEXP_REPLACE(UPPER(COALESCE(line.item_code, '')), '[^A-Z0-9]+', '', 'g'), 5) AS prefix5,
                    COUNT(DISTINCT invoice.id) AS baskets,
                    SUM(line.quantity) AS units,
                    SUM(line.price_subtotal) AS revenue,
                    MIN(invoice.invoice_date) AS first_purchase,
                    MAX(invoice.invoice_date) AS last_purchase
                FROM legacy_invoice_line line
                JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                LEFT JOIN res_partner partner ON partner.id = invoice.partner_id
                LEFT JOIN res_partner commercial ON commercial.id = partner.commercial_partner_id
                WHERE invoice.invoice_date BETWEEN %s AND %s
                  {business_sql}
                  {audience_sql}
                  AND invoice.invoice_type = 'out_invoice'
                  AND invoice.state <> 'cancel'
                  AND line.quantity > 0
                  AND {anchor_sql}
                GROUP BY partner.id, partner.name, partner.email, partner.mobile,
                         commercial.is_company, commercial.name, invoice.source_partner_id,
                         invoice.source_partner_name, invoice.source_partner_mobile,
                         invoice.source_partner_type, line.product_id,
                         COALESCE(NULLIF(line.product_name, ''), NULLIF(line.name, ''), NULLIF(line.item_code, ''), 'Product'),
                         LEFT(REGEXP_REPLACE(UPPER(COALESCE(line.item_code, '')), '[^A-Z0-9]+', '', 'g'), 5)
                ORDER BY last_purchase DESC, revenue DESC
                LIMIT 3000
                """.format(
                    anchor_sql=anchor_sql,
                    audience_sql=audience_sql,
                    business_sql=business_sql,
                ),
                [start, end, *business_params, *audience_params, *anchor_params],
            )
        columns = [column[0] for column in self.env.cr.description]
        rows = [dict(zip(columns, values)) for values in self.env.cr.fetchall()]
        current_ids = sorted({int(row["product_id"]) for row in rows if row.get("product_id")})
        if current_ids:
            names = {
                product.id: product.display_name
                for product in self.env["product.product"].sudo().browse(current_ids).exists()
            }
            for row in rows:
                row["product_name"] = names.get(row.get("product_id"), row.get("product_name"))
        return rows

    def _query_ownership_customers(self, source, query, start, end, entity, filters=None):
        """Return one exact aggregate per owner with every observed product attached."""
        anchor_sql, anchor_params = self._anchor_clause(entity, query, source=source, scoped=False)
        if source == "current":
            audience_sql, audience_params = self._audience_sql("move", filters)
            self.env.cr.execute(
                """
                WITH source_lines AS (
                    SELECT
                        'partner:' || partner.id::text AS customer_key,
                        partner.id AS partner_id,
                        partner.name,
                        partner.email,
                        partner.mobile,
                        COALESCE(commercial.is_company, FALSE) AS is_company,
                        CASE WHEN commercial.is_company THEN commercial.name ELSE NULL END AS company_name,
                        product.id AS product_id,
                        COALESCE(template.name->>'en_US', template.name->>'en', product.default_code, 'Product') AS product_name,
                        LEFT(REGEXP_REPLACE(UPPER(COALESCE(product.barcode, product.default_code, '')), '[^A-Z0-9]+', '', 'g'), 5) AS prefix5,
                        move.id AS basket_id,
                        move.invoice_date,
                        line.quantity,
                        line.price_subtotal AS revenue
                    FROM account_move_line line
                    JOIN account_move move ON move.id = line.move_id
                    JOIN product_product product ON product.id = line.product_id
                    JOIN product_template template ON template.id = product.product_tmpl_id
                    JOIN res_partner partner ON partner.id = move.partner_id
                    JOIN res_partner commercial ON commercial.id = partner.commercial_partner_id
                    WHERE move.state = 'posted'
                      AND move.move_type IN ('out_invoice', 'out_receipt')
                      AND move.invoice_date BETWEEN %s AND %s
                      AND move.company_id = ANY(%s)
                      {audience_sql}
                      AND line.quantity > 0
                      AND {anchor_sql}
                ),
                customer_totals AS (
                    SELECT customer_key, partner_id, MAX(name) AS name, MAX(email) AS email,
                           MAX(mobile) AS mobile, BOOL_OR(is_company) AS is_company,
                           MAX(company_name) AS company_name,
                           COUNT(DISTINCT basket_id) AS baskets, SUM(quantity) AS units,
                           SUM(revenue) AS revenue, MIN(invoice_date) AS first_purchase,
                           MAX(invoice_date) AS last_purchase
                    FROM source_lines
                    GROUP BY customer_key, partner_id
                ),
                product_totals AS (
                    SELECT customer_key, product_id, product_name, prefix5,
                           COUNT(DISTINCT basket_id) AS baskets, SUM(revenue) AS revenue,
                           MAX(invoice_date) AS last_purchase
                    FROM source_lines
                    GROUP BY customer_key, product_id, product_name, prefix5
                ),
                product_lists AS (
                    SELECT customer_key,
                           JSONB_AGG(
                               JSONB_BUILD_OBJECT(
                                   'product_id', product_id, 'name', product_name, 'prefix5', prefix5,
                                   'baskets', baskets, 'revenue', revenue,
                                   'last_purchase', TO_CHAR(last_purchase, 'YYYY-MM-DD')
                               ) ORDER BY last_purchase DESC, revenue DESC
                           ) AS products
                    FROM product_totals
                    GROUP BY customer_key
                )
                SELECT totals.*, lists.products
                FROM customer_totals totals
                JOIN product_lists lists USING (customer_key)
                ORDER BY totals.last_purchase DESC, totals.revenue DESC
                """.format(anchor_sql=anchor_sql, audience_sql=audience_sql),
                [start, end, self._company_ids(filters), *audience_params, *anchor_params],
            )
        else:
            if not self._has_table("legacy_invoice_line"):
                return []
            audience_sql, audience_params = self._audience_sql("invoice", filters, source="legacy")
            business_sql, business_params = self._legacy_business_sql("invoice", filters)
            self.env.cr.execute(
                """
                WITH source_lines AS (
                    SELECT
                        CASE
                            WHEN partner.id IS NOT NULL THEN 'partner:' || partner.id::text
                            WHEN invoice.source_partner_id IS NOT NULL THEN 'legacy:' || invoice.source_partner_id::text
                            WHEN NULLIF(TRIM(invoice.source_partner_mobile), '') IS NOT NULL
                                THEN 'mobile:' || REGEXP_REPLACE(invoice.source_partner_mobile, '[^0-9+]', '', 'g')
                            WHEN NULLIF(TRIM(invoice.source_partner_name), '') IS NOT NULL
                                THEN 'name:' || LOWER(TRIM(invoice.source_partner_name))
                        END AS customer_key,
                        partner.id AS partner_id,
                        COALESCE(partner.name, NULLIF(invoice.source_partner_name, ''), 'Historical customer') AS name,
                        partner.email,
                        COALESCE(partner.mobile, NULLIF(invoice.source_partner_mobile, '')) AS mobile,
                        (COALESCE(commercial.is_company, FALSE) OR LOWER(COALESCE(invoice.source_partner_type, '')) = 'company') AS is_company,
                        CASE
                            WHEN COALESCE(commercial.is_company, FALSE) THEN commercial.name
                            WHEN LOWER(COALESCE(invoice.source_partner_type, '')) = 'company' THEN invoice.source_partner_name
                        END AS company_name,
                        line.product_id,
                        COALESCE(NULLIF(line.product_name, ''), NULLIF(line.name, ''), NULLIF(line.item_code, ''), 'Product') AS product_name,
                        LEFT(REGEXP_REPLACE(UPPER(COALESCE(line.item_code, '')), '[^A-Z0-9]+', '', 'g'), 5) AS prefix5,
                        invoice.id AS basket_id,
                        invoice.invoice_date,
                        line.quantity,
                        line.price_subtotal AS revenue
                    FROM legacy_invoice_line line
                    JOIN legacy_invoice invoice ON invoice.id = line.invoice_id
                    LEFT JOIN res_partner partner ON partner.id = invoice.partner_id
                    LEFT JOIN res_partner commercial ON commercial.id = partner.commercial_partner_id
                    WHERE invoice.invoice_date BETWEEN %s AND %s
                      {business_sql}
                      {audience_sql}
                      AND invoice.invoice_type = 'out_invoice'
                      AND invoice.state <> 'cancel'
                      AND line.quantity > 0
                      AND {anchor_sql}
                ),
                identified_lines AS (
                    SELECT * FROM source_lines WHERE customer_key IS NOT NULL
                ),
                customer_totals AS (
                    SELECT customer_key, MAX(partner_id) AS partner_id, MAX(name) AS name,
                           MAX(email) AS email, MAX(mobile) AS mobile,
                           BOOL_OR(is_company) AS is_company, MAX(company_name) AS company_name,
                           COUNT(DISTINCT basket_id) AS baskets, SUM(quantity) AS units,
                           SUM(revenue) AS revenue, MIN(invoice_date) AS first_purchase,
                           MAX(invoice_date) AS last_purchase
                    FROM identified_lines
                    GROUP BY customer_key
                ),
                product_totals AS (
                    SELECT customer_key, MAX(product_id) AS product_id, product_name, prefix5,
                           COUNT(DISTINCT basket_id) AS baskets, SUM(revenue) AS revenue,
                           MAX(invoice_date) AS last_purchase
                    FROM identified_lines
                    GROUP BY customer_key, product_name, prefix5
                ),
                product_lists AS (
                    SELECT customer_key,
                           JSONB_AGG(
                               JSONB_BUILD_OBJECT(
                                   'product_id', product_id, 'name', product_name, 'prefix5', prefix5,
                                   'baskets', baskets, 'revenue', revenue,
                                   'last_purchase', TO_CHAR(last_purchase, 'YYYY-MM-DD')
                               ) ORDER BY last_purchase DESC, revenue DESC
                           ) AS products
                    FROM product_totals
                    GROUP BY customer_key
                )
                SELECT totals.*, lists.products
                FROM customer_totals totals
                JOIN product_lists lists USING (customer_key)
                ORDER BY totals.last_purchase DESC, totals.revenue DESC
                """.format(
                    anchor_sql=anchor_sql,
                    audience_sql=audience_sql,
                    business_sql=business_sql,
                ),
                [start, end, *business_params, *audience_params, *anchor_params],
            )
        columns = [column[0] for column in self.env.cr.description]
        rows = [dict(zip(columns, values)) for values in self.env.cr.fetchall()]
        product_ids = sorted(
            {
                int(product["product_id"])
                for row in rows
                for product in (row.get("products") or [])
                if product.get("product_id")
            }
        )
        display_names = {
            product.id: product.display_name
            for product in self.env["product.product"].sudo().browse(product_ids).exists()
        }
        for row in rows:
            for product in row.get("products") or []:
                product["name"] = display_names.get(product.get("product_id"), product.get("name"))
                product["last_purchase"] = (
                    fields.Date.to_date(product["last_purchase"])
                    if product.get("last_purchase")
                    else None
                )
        return rows

    def _ownership_insights(self, query, start, end, entity, filters=None, result_limit=200):
        ownership_entity = self._ownership_entity(entity)
        coverage = self._available_period(ownership_entity, ownership_entity.get("name") or query, filters)
        ownership_start = fields.Date.to_date(coverage["start_date"])
        ownership_end = fields.Date.to_date(coverage["end_date"])
        source_periods = self._unified_source_periods(ownership_start, ownership_end)
        current_rows = self._query_ownership_customers(
            "current", query, *source_periods["current"], ownership_entity, filters
        )
        legacy_rows = self._query_ownership_customers(
            "legacy", query, *source_periods["legacy"], ownership_entity, filters
        )
        owners = {}
        for source, rows in (("current", current_rows), ("legacy", legacy_rows)):
            for row in rows:
                key = self._customer_identity(row)
                owner = owners.setdefault(
                    key,
                    {
                        "customer_key": key,
                        "partner_id": row.get("partner_id") or 0,
                        "name": row.get("name") or "Customer",
                        "email": row.get("email") or "",
                        "mobile": row.get("mobile") or "",
                        "is_company": bool(row.get("is_company")),
                        "company_name": row.get("company_name") or "",
                        "baskets": 0,
                        "units": 0.0,
                        "revenue": 0.0,
                        "first_purchase": None,
                        "last_purchase": None,
                        "products": {},
                        "evidence_sources": [],
                    },
                )
                owner["email"] = owner["email"] or row.get("email") or ""
                owner["mobile"] = owner["mobile"] or row.get("mobile") or ""
                owner["company_name"] = owner["company_name"] or row.get("company_name") or ""
                owner["baskets"] += int(row.get("baskets") or 0)
                owner["units"] += float(row.get("units") or 0.0)
                owner["revenue"] += float(row.get("revenue") or 0.0)
                if row.get("first_purchase") and (
                    not owner["first_purchase"] or row["first_purchase"] < owner["first_purchase"]
                ):
                    owner["first_purchase"] = row["first_purchase"]
                if row.get("last_purchase") and (
                    not owner["last_purchase"] or row["last_purchase"] > owner["last_purchase"]
                ):
                    owner["last_purchase"] = row["last_purchase"]
                for source_product in row.get("products") or []:
                    product_key = source_product.get("prefix5") or (source_product.get("name") or "").lower()
                    product = owner["products"].setdefault(
                        product_key,
                        {
                            "name": source_product.get("name") or "Product",
                            "prefix5": source_product.get("prefix5") or "",
                            "baskets": 0,
                            "revenue": 0.0,
                            "last_purchase": None,
                        },
                    )
                    product["baskets"] += int(source_product.get("baskets") or 0)
                    product["revenue"] += float(source_product.get("revenue") or 0.0)
                    if source_product.get("last_purchase") and (
                        not product["last_purchase"] or source_product["last_purchase"] > product["last_purchase"]
                    ):
                        product["last_purchase"] = source_product["last_purchase"]
                if source not in owner["evidence_sources"]:
                    owner["evidence_sources"].append(source)

        selected_generation = self._iphone_generation(entity.get("name") or query)
        target_generation = selected_generation + 1 if selected_generation else 0
        max_revenue = max((row["revenue"] for row in owners.values()), default=1.0) or 1.0
        ready = reachable = 0
        output = []
        for owner in owners.values():
            products = sorted(
                owner.pop("products").values(),
                key=lambda product: (product.get("last_purchase") or date.min, product["revenue"]),
                reverse=True,
            )
            observed_generations = [self._iphone_generation(product["name"]) for product in products]
            observed_generation = max(observed_generations, default=0)
            last_purchase = owner.get("last_purchase")
            age_days = max((ownership_end - last_purchase).days, 0) if last_purchase else 0
            generation_gap = max(target_generation - observed_generation, 0) if target_generation and observed_generation else 0
            contactable = bool(owner.get("email") or owner.get("mobile"))
            score = min(
                100,
                round(
                    min(generation_gap, 4) / 4.0 * 45.0
                    + min(age_days, 730) / 730.0 * 25.0
                    + (15.0 if contactable else 0.0)
                    + owner["revenue"] / max_revenue * 15.0
                ),
            )
            opportunity = "High" if score >= 65 else ("Medium" if score >= 40 else "Watch")
            if opportunity in {"High", "Medium"}:
                ready += 1
            if contactable:
                reachable += 1
            owner.update(
                {
                    "owned_product": products[0]["name"] if products else "Product owner",
                    "owned_products": [product["name"] for product in products[:5]],
                    "generation": observed_generation,
                    "generation_gap": generation_gap,
                    "upgrade_score": score,
                    "upgrade_opportunity": opportunity,
                    "upgrade_reason": (
                        f"{generation_gap} generation{'s' if generation_gap != 1 else ''} behind next target"
                        if generation_gap
                        else ("Older purchase cycle" if age_days >= 365 else "Monitor purchase cycle")
                    ),
                    "reachability": "Email + mobile" if owner.get("email") and owner.get("mobile") else (
                        "Email" if owner.get("email") else ("Mobile" if owner.get("mobile") else "Needs enrichment")
                    ),
                    "last_purchase": fields.Date.to_string(last_purchase) if last_purchase else "",
                    "first_purchase": fields.Date.to_string(owner.get("first_purchase")) if owner.get("first_purchase") else "",
                    "revenue": round(owner["revenue"], 2),
                }
            )
            output.append(owner)
        output.sort(key=lambda row: (-row["upgrade_score"], -row["revenue"], row["name"]))
        requested_limit = int(result_limit) if result_limit is not None else 200
        visible_customers = output if requested_limit <= 0 else output[: max(requested_limit, 1)]
        return {
            "category_name": ownership_entity.get("name") or "Selected category",
            "target_generation": target_generation,
            "rule_label": "Evidence-based upgrade opportunity",
            "rule_note": "Ranks generation gap, purchase age, observed value and contact readiness; no inferred ownership is included.",
            "summary": {
                "identified_owners": len(output),
                "upgrade_ready": ready,
                "reachable": reachable,
            },
            "customers": visible_customers,
            "coverage": coverage,
        }

    @staticmethod
    def _bounded_probability(successes, observations):
        """Return a safe basket probability for sparse or imperfect imports.

        Companion queries enforce distinct basket counts and normalize duplicate
        catalog rows by their five-character prefix.  This final guard keeps the
        statistical layer defined if a future import violates that invariant.
        """
        observations = int(observations or 0)
        if observations <= 0:
            return 0.0
        probability = float(successes or 0) / observations
        return min(1.0, max(0.0, probability))

    @api.model
    def get_product_360(
        self,
        query,
        start_date=None,
        end_date=None,
        source="auto",
        limit=20,
        entity=None,
        filters=None,
        ownership_limit=200,
        include_ownership=True,
    ):
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
            source_used = "unified" if counts["legacy"] and counts["current"] else (
                "legacy" if counts["legacy"] else "current"
            )
        else:
            source_used = source
        if source_used == "unified":
            source_periods = self._unified_source_periods(start, end)
            current_result = (
                *self._query_current(query, *source_periods["current"], int(limit or 20), entity, filters),
                self._query_current_dimensions(query, *source_periods["current"], entity, filters),
            )
            legacy_result = (
                *self._query_legacy(query, *source_periods["legacy"], int(limit or 20), entity, filters),
                self._query_legacy_dimensions(query, *source_periods["legacy"], entity, filters),
            )
            companions, customers, payments, dimensions = self._merge_source_results(
                current_result, legacy_result, int(limit or 20)
            )
        elif source_used == "legacy":
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
        if source_used == "unified":
            identified_customers = self._deduplicated_customer_count(
                query, start, end, entity, filters, source_used, source_periods
            )
            dimensions.setdefault("scope_summary", {})["identified_customers"] = identified_customers
        for row in companions:
            co_baskets = int(row.get("co_baskets") or 0)
            base_baskets = int(row.get("base_baskets") or 0)
            probability = self._bounded_probability(co_baskets, baskets)
            attach_rate = probability * 100.0
            base_rate = (base_baskets / all_baskets) if all_baskets else 0.0
            lift = (probability / base_rate) if base_rate else 0.0
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
        companion_baskets = int(
            first.get("companion_baskets")
            or scope_summary.get("companion_baskets")
            or 0
        )
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
            "legacy_variant": "Historical SKU / prefix-5",
            "query": "Search match",
        }
        identity_summary = self._product_identity_summary(entity)
        if entity["type"] == "variant" and identity_summary["identity_precision"] == "prefix_family":
            grain_labels["variant"] = "Exact current variant + historical prefix family"
        recommendation = {
            "title": f"Bundle {display_name} + {top['product_name']}" if top else "Expand the date range",
            "rationale": "Highest attach volume with meaningful lift" if top else "No companion signal is available for this scope.",
            "reachable_baskets": int(top.get("co_baskets") or 0) if top else 0,
        }
        ownership = (
            self._ownership_insights(
                query, start, end, entity, filters, result_limit=ownership_limit
            )
            if include_ownership
            else {"summary": {}, "customers": [], "coverage": {}, "loading": True}
        )
        available_period = self._available_period(entity, query, filters)
        bundle = {
            "product": {
                "name": display_name,
                "query": query,
                "type": entity["type"],
                "id": entity["id"],
                "grain_label": grain_labels[entity["type"]],
                **identity_summary,
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
            "available_period": available_period,
            "ownership": ownership,
            "source_requested": source,
            "source_used": source_used,
            "source_label": self.SOURCE_LABELS[source_used],
            "filters": {
                "customer_type": filters["customer_type"],
                "customer_company_id": filters["customer_company_id"],
                "operating_company_id": filters["operating_company_id"],
                "operating_company_name": filters["operating_company_name"],
            },
        }
        return self._transport_safe(bundle)

    @api.model
    def get_ownership_insights(self, query, entity=None, filters=None, limit=200):
        """Load the larger category-owner population independently from basket KPIs."""
        self._ensure_access()
        query = (query or "").strip()
        normalized = self._normalize_entity(entity, query)
        if normalized["type"] == "query" and len(query) < 2:
            raise UserError("Choose an exact product before building an owner audience.")
        return self._transport_safe(
            self._ownership_insights(
                query,
                date(2025, 1, 1),
                fields.Date.today(),
                normalized,
                self._normalize_filters(filters),
                result_limit=limit,
            )
        )

    def _evidence_anchor_domain(self, entity, query, source):
        line_prefix = "invoice_line_ids" if source == "current" else "line_ids"
        if entity["type"] == "legacy_variant" and entity.get("prefixes"):
            prefix = entity["prefixes"][0]
            if source == "current":
                return expression.OR(
                    [
                        [(f"{line_prefix}.product_id.barcode", "ilike", prefix)],
                        [(f"{line_prefix}.product_id.default_code", "ilike", prefix)],
                    ]
                )
            return [(f"{line_prefix}.item_code", "ilike", prefix)]
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
                ("invoice_type", "=", "out_invoice"),
                ("state", "!=", "cancel"),
            ] + self._legacy_business_domain(filters) + legacy_customer_domain + self._evidence_anchor_domain(entity, query, "legacy")
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
                "name": f"Evidence · {entity['name'] or query} · Historical sales",
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
            ("company_id", "in", self._company_ids(filters)),
        ] + current_customer_domain + self._evidence_anchor_domain(entity, query, "current")
        if companion_key and str(companion_key).isdigit():
            domain.append(("invoice_line_ids.product_id", "=", int(companion_key)))
        return {
            "type": "ir.actions.act_window",
            "name": f"Evidence · {entity['name'] or query} · Current operations",
            "res_model": "account.move",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": domain,
            "target": "current",
            "context": {"search_default_group_by_invoice_date": 1},
        }

    @api.model
    def open_owner_evidence(self, query, source, entity=None, customer_key=None, filters=None):
        """Open category ownership invoices for one identified customer."""
        self._ensure_access()
        query = (query or "").strip()
        normalized = self._normalize_entity(entity, query)
        ownership_entity = self._ownership_entity(normalized)
        period = self._available_period(
            ownership_entity, ownership_entity.get("name") or query, filters
        )
        action = self.open_evidence(
            ownership_entity.get("name") or query,
            period["start_date"],
            period["end_date"],
            source,
            ownership_entity,
            None,
            filters,
        )
        key = str(customer_key or "")
        if key.startswith("partner:") and key.split(":", 1)[1].isdigit():
            partner_id = int(key.split(":", 1)[1])
            action["domain"].append(("partner_id", "=", partner_id))
        elif key.startswith("email:"):
            email = key.split(":", 1)[1]
            field_name = "partner_id.email"
            action["domain"].append((field_name, "=ilike", email))
        elif key.startswith("mobile:"):
            mobile = key.split(":", 1)[1]
            if source == "legacy":
                action["domain"] += expression.OR(
                    [[("source_partner_mobile", "ilike", mobile)], [("partner_id.mobile", "ilike", mobile)]]
                )
            else:
                action["domain"].append(("partner_id.mobile", "ilike", mobile))
        elif key.startswith("name:"):
            name = key.split(":", 1)[1]
            field_name = "source_partner_name" if source == "legacy" else "partner_id.name"
            action["domain"].append((field_name, "=ilike", name))
        action["name"] = f"Owner evidence · {ownership_entity.get('name') or query}"
        return action

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
        if export_mode not in {"current_view", "detail_rows", "customers", "ownership", "legacy_live"}:
            export_mode = "current_view"
        # Exports must contain the complete evidence-backed owner population. The
        # interactive dashboard remains deliberately paged via its own limit.
        bundle = self.get_product_360(
            query, start_date, end_date, source, 100, entity, filters, 0, True
        )
        comparison = self.get_legacy_comparison(query, entity, filters)
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
                ("Operating company", bundle["filters"]["operating_company_name"] or "All businesses"),
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

        if export_mode in {"current_view", "detail_rows", "ownership"}:
            sheet = workbook.add_worksheet("Owner Upgrade Audience")
            sheet.set_column("A:D", 28)
            sheet.set_column("E:M", 19)
            ownership = bundle.get("ownership") or {}
            owner_rows = [
                [
                    customer["name"],
                    "Company" if customer.get("is_company") else "Individual",
                    customer.get("company_name") or "",
                    customer.get("owned_product") or "",
                    customer.get("generation") or "",
                    customer.get("last_purchase") or "",
                    customer.get("revenue") or 0.0,
                    customer.get("upgrade_opportunity") or "",
                    customer.get("upgrade_score") or 0,
                    customer.get("upgrade_reason") or "",
                    customer.get("reachability") or "",
                    customer.get("email") or "",
                    customer.get("mobile") or "",
                ]
                for customer in ownership.get("customers", [])
            ]
            sheet.write(0, 0, "Owner & upgrade opportunities", title)
            sheet.write(1, 0, "Category scope", header)
            sheet.write(1, 1, ownership.get("category_name") or "Selected category", cell)
            sheet.write(2, 0, "Scoring rule", header)
            sheet.write(2, 1, ownership.get("rule_note") or "", cell)
            write_table(
                sheet,
                4,
                [
                    "Customer", "Customer type", "Company", "Owned product", "Generation",
                    "Last purchase", "Observed value", "Upgrade opportunity", "Upgrade score",
                    "Upgrade reason", "Reachability", "Email", "Mobile",
                ],
                owner_rows,
                {6: money},
            )

        if export_mode in {"current_view", "legacy_live"}:
            sheet = workbook.add_worksheet("Sales Timeline")
            sheet.set_column("A:A", 22)
            sheet.set_column("B:F", 20)
            sheet.write(0, 0, "Sales history and current performance", title)
            if comparison.get("available"):
                sheet.write(2, 0, "Cross-version identity", header)
                sheet.write(2, 1, comparison["rule_label"], cell)
                sheet.write(3, 0, "Compared prefixes", header)
                sheet.write(3, 1, comparison["prefix_count"], cell)
                sheet.write(4, 0, "Variant identity status", header)
                sheet.write(4, 1, comparison["identity_state_label"], cell)
                identity_rows = [
                    [
                        row["prefix5"], row["state_label"], row["legacy_name"], row["legacy_item_code"],
                        row["legacy_variant_count"], row["current_name"], row["current_item_code"],
                        row["current_catalog_status"], row["current_active_variant_count"], row["current_variant_count"],
                    ]
                    for row in comparison["identity_rows"]
                ]
                next_row = write_table(
                    sheet, 6,
                    ["Prefix-5", "State", "Historical product", "Historical item code", "Historical variants", "Current product", "Current item code", "Current status", "Current active variants", "Current total variants"],
                    identity_rows,
                )
                rows = [
                    [period["label"], period["legacy_qty"], period["legacy_amount"], period["current_qty"], period["current_amount"], period.get("amount_delta_pct")]
                    for period in comparison["months"]
                ]
                write_table(sheet, next_row, ["Month", "2025 units", "2025 revenue", "2026 units", "2026 revenue", "Revenue change %"], rows, {2: money, 4: money})
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

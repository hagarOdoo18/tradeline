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

    def _query_current(self, query, start, end, limit):
        pattern = f"%{query.strip()}%"
        company_ids = self._company_ids()
        self.env.cr.execute(
            """
            WITH scope_lines AS (
                SELECT
                    move.id AS basket_id,
                    move.partner_id,
                    move.invoice_date,
                    line.product_id,
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
                WHERE product_name ILIKE %s OR item_code ILIKE %s
            ),
            anchor_names AS (
                SELECT product_name, COUNT(DISTINCT basket_id) AS baskets
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND (product_name ILIKE %s OR item_code ILIKE %s)
                GROUP BY product_name
                ORDER BY baskets DESC, product_name
                LIMIT 1
            ),
            basket_companions AS (
                SELECT DISTINCT basket_id, product_id, product_name
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND NOT (product_name ILIKE %s OR item_code ILIKE %s)
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
            """,
            [
                start,
                end,
                company_ids,
                pattern,
                pattern,
                pattern,
                pattern,
                pattern,
                pattern,
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
                WHERE product_name ILIKE %s OR item_code ILIKE %s
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
            """,
            [start, end, company_ids, pattern, pattern],
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
                  AND (
                    COALESCE(template.name->>'en_US', template.name->>'en', '') ILIKE %s
                    OR COALESCE(product.default_code, '') ILIKE %s
                  )
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
            """,
            [start, end, company_ids, pattern, pattern],
        )
        payments = [{"name": row[0], "baskets": row[1]} for row in self.env.cr.fetchall()]
        return companions, customers, payments

    def _query_legacy(self, query, start, end, limit):
        if not self._has_table("legacy_invoice_line"):
            return [], [], []
        pattern = f"%{query.strip()}%"
        company_ids = self._company_ids()
        self.env.cr.execute(
            """
            WITH scope_lines AS (
                SELECT
                    invoice.id AS basket_id,
                    invoice.partner_id,
                    invoice.invoice_date,
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
                WHERE product_name ILIKE %s OR item_code ILIKE %s
            ),
            anchor_names AS (
                SELECT product_name, COUNT(DISTINCT basket_id) AS baskets
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND (product_name ILIKE %s OR item_code ILIKE %s)
                GROUP BY product_name
                ORDER BY baskets DESC, product_name
                LIMIT 1
            ),
            basket_companions AS (
                SELECT DISTINCT basket_id, product_key, product_name
                FROM scope_lines
                WHERE basket_id IN (SELECT basket_id FROM anchor_baskets)
                  AND NOT (product_name ILIKE %s OR item_code ILIKE %s)
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
            """,
            [start, end, company_ids, pattern, pattern, pattern, pattern, pattern, pattern, limit],
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
                  AND (COALESCE(line.product_name, line.name, '') ILIKE %s OR COALESCE(line.item_code, '') ILIKE %s)
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
            """,
            [start, end, company_ids, pattern, pattern],
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
                  AND (COALESCE(line.product_name, line.name, '') ILIKE %s OR COALESCE(line.item_code, '') ILIKE %s)
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
            """,
            [start, end, company_ids, pattern, pattern],
        )
        payments = [{"name": row[0], "baskets": row[1]} for row in self.env.cr.fetchall()]
        return companions, customers, payments

    def _source_counts(self, query, start, end):
        pattern = f"%{query.strip()}%"
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
              AND (COALESCE(template.name->>'en_US', template.name->>'en', '') ILIKE %s OR COALESCE(product.default_code, '') ILIKE %s)
            """,
            [start, end, company_ids, pattern, pattern],
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
                  AND (COALESCE(line.product_name, line.name, '') ILIKE %s OR COALESCE(line.item_code, '') ILIKE %s)
                """,
                [start, end, company_ids, pattern, pattern],
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
    def search_products(self, query, limit=12):
        self._ensure_access()
        query = (query or "").strip()
        if len(query) < 2:
            return []
        output = []
        seen = set()
        for product_id, name in self.env["product.product"].sudo().name_search(query, operator="ilike", limit=limit):
            key = name.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            output.append({"key": f"current:{product_id}", "name": name, "source": "current"})
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
                seen.add(label.strip().lower())
                output.append({"key": f"legacy:{len(output)}", "name": label, "source": "legacy"})
                if len(output) >= limit:
                    break
        return output

    @api.model
    def get_product_360(self, query, start_date=None, end_date=None, source="auto", limit=20):
        self._ensure_access()
        query = (query or "").strip()
        if len(query) < 2:
            raise UserError("Choose a product or enter at least two search characters.")
        start, end = self._date_range(start_date, end_date)
        counts = self._source_counts(query, start, end)
        if source not in {"auto", "current", "legacy"}:
            source = "auto"
        if source == "auto":
            source_used = "legacy" if counts["legacy"] else "current"
        else:
            source_used = source
        if source_used == "legacy":
            companions, customers, payments = self._query_legacy(query, start, end, int(limit or 20))
        else:
            companions, customers, payments = self._query_current(query, start, end, int(limit or 20))

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
        recommendation = {
            "title": f"Bundle {first.get('anchor_name') or query} + {top['product_name']}" if top else "Expand the date range",
            "rationale": "Highest attach volume with meaningful lift" if top else "No companion signal is available for this scope.",
            "reachable_baskets": int(top.get("co_baskets") or 0) if top else 0,
        }
        return {
            "product": {"name": first.get("anchor_name") or query, "query": query},
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
    def export_product_insight(self, query, start_date=None, end_date=None, source="auto"):
        self._ensure_access()
        bundle = self.get_product_360(query, start_date, end_date, source, 100)
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["Tradeline Product Intelligence", bundle["product"]["name"]])
        writer.writerow(["Data source", bundle["source_label"]])
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

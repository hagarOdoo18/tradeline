"""Catalogue discovery and fixed, permission-checked product unit accounting."""
from datetime import datetime, time, timedelta

import pytz
from psycopg2.errors import QueryCanceled

from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


class CopilotProductSales(models.AbstractModel):
    _inherit = "ai.copilot.query"

    def search_products(self, search, limit=100):
        self._service()._assert_internal_user()
        if not isinstance(search, str) or not 2 <= len(search.strip()) <= 160:
            raise ValidationError("Supply a short product name or code.")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValidationError("Use a product discovery limit from 1 to 500.")
        meta = self._readable_fields("product.product")
        if not {"id", "name", "display_name", "categ_id", "active", "uom_id"} <= meta.keys():
            raise AccessError("Product names, categories, status and units must be readable for product discovery.")
        keys = [key for key in ("name", "default_code", "barcode") if key in meta]
        domain = ["|"] * (len(keys) - 1) + [[key, "ilike", search.strip()] for key in keys]
        result = self.execute_query({"model": "product.product", "domain": domain,
            "fields": [key for key in ("id", "display_name", "name", "default_code", "categ_id", "active", "uom_id") if key in meta],
            "include_archived": True, "limit": limit, "title": "Products matching " + search.strip()})
        result["title"] = result["query"]["title"]
        result["catalogue_guidance"] = (
            "Includes archived variants for historical purchases. Resolve the user's named product using names, categories and codes. "
            "A named product is not a generic hardware feature. Do not ask about dual-SIM merely because a product is called Duo. "
            "Keep the user's requested product scope; inspect ambiguous accessory/demo/quantity-placeholder entries before selecting IDs. "
            "If truncated, narrow the product query before claiming the selection is complete.")
        return result

    def _unit_dates(self, spec):
        start, end = spec.get("date_from"), spec.get("date_to")
        try:
            start, end = fields.Date.to_date(start), fields.Date.to_date(end)
        except (ValueError, TypeError) as exc:
            raise ValidationError("Use YYYY-MM-DD dates or omit both dates for all available records.") from exc
        if bool(start) != bool(end) or (start and start > end):
            raise ValidationError("Supply both dates in order, or omit both for all available records.")
        return start, end

    def _unit_source(self, name, domain, quantity, groups, export):
        model, unused = self._analysis_model(name, export)
        domain = self._analysis_domain(name, domain, export)
        needed = {quantity, *groups}
        if not needed <= self._readable_fields(name).keys():
            raise AccessError("Sales quantity, unit or date fields are unavailable under your policy.")
        with self._budget():
            rows = model.read_group(domain, [quantity + ":sum"], groups, lazy=False, limit=1001)
        if len(rows) > 1000:
            raise ValidationError("Too many sales/unit groups; narrow the selected products.")
        return rows

    def product_sales_units(self, spec, export=False):
        self._service()._assert_internal_user()
        allowed = {"analysis_type", "model", "product_ids", "date_from", "date_to", "legacy_search", "title", "chart", "limit"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValidationError("Invalid product sales definition.")
        ids = spec.get("product_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(type(v) is not int or v <= 0 for v in ids):
            raise ValidationError("Select one to 500 visible product IDs from catalogue discovery.")
        term = spec.get("legacy_search", "")
        if not isinstance(term, str) or len(term) > 160:
            raise ValidationError("Use a short legacy product search label.")
        start, end = self._unit_dates(spec)
        # Historical units must include archived variants; this never changes ACLs/rules.
        product, unused = self._analysis_model("product.product", export)
        product = product.with_context(active_test=False)
        if not {"id", "uom_id"} <= self._readable_fields(product._name).keys():
            raise AccessError("Product IDs and units must be readable.")
        with self._budget():
            products = product.search_read([("id", "in", ids)], ["uom_id"], limit=501)
        if {p["id"] for p in products} != set(ids):
            raise AccessError("One or more selected products are unavailable under your access rules.")
        units, unused = self._model("uom.uom")
        units = units.with_context(active_test=False)
        if not {"factor", "category_id", "rounding", "display_name"} <= self._readable_fields("uom.uom").keys():
            raise AccessError("Unit conversion fields are unavailable under your data policy.")
        unit_ids = {p["uom_id"][0] for p in products if p.get("uom_id")}
        with self._budget():
            visible_units = units.search([("id", "in", list(unit_ids))])
            if not unit_ids or set(visible_units.ids) != unit_ids or len(visible_units.mapped("category_id")) != 1:
                raise ValidationError("Selected products need readable units from one unit category; report different unit categories separately.")
            target = visible_units.sorted("id")[:1]
        product_units = {p["id"]: p["uom_id"][0] for p in products}
        coverage, rows = [], []
        totals = {}

        def dates(field, datetime_field=False):
            if not start:
                return []
            if not datetime_field:
                return [(field, ">=", str(start)), (field, "<=", str(end))]
            tz = pytz.timezone(self.env.context.get("tz") or self.env.user.tz or "UTC")
            lower = tz.localize(datetime.combine(start, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
            upper = tz.localize(datetime.combine(end + timedelta(days=1), time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
            return [(field, ">=", fields.Datetime.to_string(lower)), (field, "<", fields.Datetime.to_string(upper))]

        def converted(value, unit_id):
            if not unit_id:
                raise ValidationError("A matching sales line has no unit of measure; a verified unit total is unavailable.")
            with self._budget():
                unit = units.search([("id", "=", unit_id)], limit=1)
                if not unit or unit.category_id != target.category_id:
                    raise ValidationError("A sales unit cannot be converted to the selected product unit.")
                return unit._compute_quantity(value or 0, target, round=False)

        definitions = [
            ("invoices", "Posted customer invoices less credit notes", "account.move.line", "quantity",
             ["move_type", "product_uom_id"], "move_id.invoice_date",
             [("product_id", "in", ids), ("parent_state", "=", "posted"),
              ("move_type", "in", ["out_invoice", "out_refund"]), ("display_type", "=", "product")] + dates("move_id.invoice_date")),
            ("pos", "Completed POS without an invoice link", "pos.order.line", "qty", ["product_id"], "order_id.date_order",
             [("product_id", "in", ids), ("order_id.state", "in", ["paid", "done", "invoiced"]),
              ("order_id.account_move", "=", False)] + dates("order_id.date_order", True)),
            ("legacy", "Mapped legacy invoices less credit notes (separate)", "legacy.invoice.line", "quantity",
             ["invoice_type", "uom_id"], "invoice_date",
             [("product_id", "in", ids), ("state", "in", ["open", "paid"]),
              ("invoice_type", "in", ["out_invoice", "out_refund"])] + dates("invoice_date")),
        ]
        for key, label, name, quantity, groups, date_field, domain in definitions:
            detail = {"source": key, "model": name, "domain": domain, "status": "unavailable"}
            try:
                with self.env.cr.savepoint():
                    if name not in self.env:
                        raise ValidationError("Source is not installed.")
                    grouped = self._unit_source(name, domain, quantity, groups, export)
                    net, count = 0.0, 0
                    for row in grouped:
                        unit_id = product_units[row["product_id"][0]] if key == "pos" else (row.get(groups[1]) or [None])[0]
                        sign = -1 if row.get(groups[0]) == "out_refund" else 1
                        net += sign * converted(row[quantity], unit_id)
                        count += row["__count"]
                    detail.update(status="ok", matching_lines=count, net_units=net)
                    # These are global source watermarks, not proof of gap-free history.
                    if key == "legacy":
                        header, unused = self._analysis_model("legacy.invoice", export)
                        header_domain = [("state", "in", ["open", "paid"]), ("invoice_type", "in", ["out_invoice", "out_refund"])]
                        self._domain(header._name, [["invoice_date", "!=", False]] + header_domain)
                        with self._budget():
                            marks = header.read_group(header_domain,
                                ["first_date:min(invoice_date)", "last_date:max(invoice_date)"], [], lazy=False)
                        detail["visible_archive_window"] = {k: marks[0].get(k) for k in ("first_date", "last_date")} if marks else {}
                        if term.strip():
                            # Unmapped text hits are candidates, never silently treated as handset sales or as zero.
                            candidate_domain = [("product_id", "=", False), ("name", "ilike", term.strip()),
                                ("state", "in", ["open", "paid"]), ("invoice_type", "in", ["out_invoice", "out_refund"])] + dates("invoice_date")
                            candidate_model, unused = self._analysis_model(name, export)
                            candidate_domain = self._analysis_domain(name, candidate_domain, export)
                            with self._budget():
                                detail["unmapped_text_candidate_lines"] = candidate_model.search_count(candidate_domain)
                    totals[key] = net
                    rows.append({"channel": label, "net_units": net, "unit": target.display_name, "matching_lines": count})
            except (AccessError, ValidationError) as exc:
                detail.update(status="unavailable", reason=str(exc)[:400])
            except QueryCanceled:
                detail.update(status="unavailable", reason="Source exceeded the eight-second query budget; absence cannot be inferred.")
            coverage.append(detail)
        if not rows:
            raise ValidationError("No sales source could be checked under your current permissions.")
        complete = all(c["status"] == "ok" for c in coverage)
        current = totals["invoices"] + totals["pos"] if {"invoices", "pos"} <= totals.keys() else None
        scope = ("Unit quantities, not line/document counts. Posted customer invoices minus credit notes; completed POS "
            "with no invoice link is separate, preventing invoiced POS from being counted twice. Sales orders and Shopify "
            "orders are not added. All quantities converted to the displayed product unit. Archived variants included. "
            "Legacy totals remain separate: migration overlap and missing history are not reconciled. A zero only means "
            "no matching visible records in that source, never no historic sales. Source failures are unavailable, not zero.")
        result = self._analysis_result(dict(spec, analysis_type="product_units"), "product.product", "Product handset/unit sales",
            rows, ["channel", "net_units", "unit", "matching_lines"],
            {"channel": "Sales source", "net_units": "Net units", "unit": "Unit", "matching_lines": "Matching lines"}, coverage, scope, export)
        result["analysis"].update(current_net_units=current, legacy_net_units=totals.get("legacy"),
            source_checks_complete=complete, lifetime_history_complete=False,
            legacy_product_coverage_complete=bool(term.strip()) and coverage[2]["status"] == "ok"
                and not coverage[2].get("unmapped_text_candidate_lines"),
            period={"from": str(start) if start else None, "to": str(end) if end else None,
                    "basis": "Requested dates" if start else "All available saved records; complete lifetime history not certified"})
        result.update(chart_dimensions=["channel"], chart_measures=["net_units"])
        return result

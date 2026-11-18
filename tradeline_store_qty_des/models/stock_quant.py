# -*- coding: utf-8 -*-
import base64
from collections import defaultdict
from datetime import datetime, time
from io import BytesIO

import xlsxwriter

from odoo import _, fields, models
from odoo.exceptions import UserError

# Order states counted as a realised sale.
POS_SOLD_STATES = ("paid", "done", "invoiced")
SALE_SOLD_STATES = ("sale",)


class StoreDesReportWizard(models.TransientModel):
    _name = "store.des.report.wizard"
    _description = "Store Product Description Quantity Excel Wizard"

    date_from = fields.Date(
        string="Sold From",
        help="Start of the period used to count sold quantity (POS + Sale). "
             "Leave empty to count all history. On-hand quantity is always current.",
    )
    date_to = fields.Date(
        string="Sold To",
        help="End of the period used to count sold quantity (inclusive).",
    )
    gentextfile = fields.Binary(string="Report", readonly=True)
    filename = fields.Char(string="File name", readonly=True)

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------
    def action_generate_warranty_report(self):
        self.ensure_one()

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError(_("'Sold From' must be before 'Sold To'."))

        stores = self._get_store_locations()
        if not stores:
            raise UserError(_("No active internal locations were found."))

        data = self._collect_report_data(stores)
        if not data:
            raise UserError(_("No products with stock on hand were found."))

        content = self._build_workbook(stores, data)
        filename = "Store Product Description Quant Sheet.xlsx"
        self.write({
            "gentextfile": base64.b64encode(content),
            "filename": filename,
        })

        return {
            "type": "ir.actions.act_url",
            "name": "Store Product Quant Sheet",
            "url": "/web/content/%s/%s/gentextfile/%s?download=true"
                   % (self._name, self.id, filename),
            "target": "new",
        }

    # ------------------------------------------------------------------
    # Data gathering (ORM)
    # ------------------------------------------------------------------
    def _get_store_locations(self):
        """Active internal stock locations, i.e. the report's store columns."""
        Location = self.env["stock.location"]
        locations = Location.search([("usage", "=", "internal"), ("active", "=", True)])
        # The legacy module ordered by a custom 'location_number' column that no
        # longer exists in the code base; fall back to complete_name.
        if "location_number" in Location._fields:
            return locations.sorted(key=lambda l: (l.location_number or 0, l.complete_name or ""))
        return locations.sorted(key=lambda l: l.complete_name or "")

    def _sold_date_domain(self):
        domain = []
        if self.date_from:
            domain.append(("order_id.date_order", ">=",
                           datetime.combine(self.date_from, time.min)))
        if self.date_to:
            domain.append(("order_id.date_order", "<=",
                           datetime.combine(self.date_to, time.max)))
        return domain

    def _collect_report_data(self, stores):
        """Build, per product template, its metadata, on-hand per store and
        sold (POS + Sale) per store.

        Returns a list of dicts ordered by description, one per template that
        has non-zero on-hand in the listed stores (legacy behaviour)."""
        store_ids = stores.ids

        # --- On-hand per (product, location) via stock.quant --------------
        quants = self.env["stock.quant"].search([("location_id", "in", store_ids)])
        onhand = defaultdict(float)          # (tmpl_id, loc_id) -> qty
        onhand_total = defaultdict(float)    # tmpl_id -> qty
        tmpl_variants = defaultdict(lambda: self.env["product.product"])
        for q in quants:
            product = q.product_id
            tmpl = product.product_tmpl_id
            onhand[(tmpl.id, q.location_id.id)] += q.quantity
            onhand_total[tmpl.id] += q.quantity
            tmpl_variants[tmpl.id] |= product

        # Keep only templates that actually carry stock (legacy: qty != 0).
        tmpl_ids = [tid for tid, qty in onhand_total.items() if qty]
        if not tmpl_ids:
            return []

        # All variants we care about, for the sold-qty searches.
        variant_ids = []
        for tid in tmpl_ids:
            variant_ids += tmpl_variants[tid].ids
        variant_to_tmpl = {}
        for tid in tmpl_ids:
            for v in tmpl_variants[tid]:
                variant_to_tmpl[v.id] = tid

        # --- Sold per (template, location): POS order lines ---------------
        sold = defaultdict(float)        # (tmpl_id, loc_id) -> qty
        sold_total = defaultdict(float)  # tmpl_id -> qty
        date_dom = self._sold_date_domain()

        pos_lines = self.env["pos.order.line"].search(
            [("product_id", "in", variant_ids),
             ("order_id.state", "in", list(POS_SOLD_STATES))] + date_dom)
        for line in pos_lines:
            tid = variant_to_tmpl.get(line.product_id.id)
            if tid is None:
                continue
            picking_type = line.order_id.config_id.picking_type_id
            loc = picking_type.default_location_src_id
            sold_total[tid] += line.qty
            if loc and loc.id in store_ids:
                sold[(tid, loc.id)] += line.qty

        # --- Sold per (template, location): Sale order lines --------------
        sale_lines = self.env["sale.order.line"].search(
            [("product_id", "in", variant_ids),
             ("order_id.state", "in", list(SALE_SOLD_STATES))] + date_dom)
        for line in sale_lines:
            tid = variant_to_tmpl.get(line.product_id.id)
            if tid is None:
                continue
            qty = line.qty_delivered
            if not qty:
                continue
            loc = line.order_id.warehouse_id.lot_stock_id
            sold_total[tid] += qty
            if loc and loc.id in store_ids:
                sold[(tid, loc.id)] += qty

        # --- Assemble rows ------------------------------------------------
        templates = self.env["product.template"].browse(tmpl_ids)
        rows = []
        for tmpl in templates:
            rows.append({
                "category": tmpl.categ_id.name or "",
                "family": tmpl.family_id.name or "",
                "description": tmpl.name or "",
                "price": self._price_with_taxes(tmpl),
                "total_qty": onhand_total[tmpl.id],
                "total_sold": sold_total.get(tmpl.id, 0.0),
                "onhand": {loc.id: onhand.get((tmpl.id, loc.id), 0.0) for loc in stores},
                "sold": {loc.id: sold.get((tmpl.id, loc.id), 0.0) for loc in stores},
            })
        rows.sort(key=lambda r: (r["description"] or "").lower())
        return rows

    def _price_with_taxes(self, tmpl):
        """Tax-inclusive sales price.

        Replaces the legacy 'price_with_taxes' column (no longer defined in
        the code base) by computing it from the product's customer taxes."""
        price = tmpl.list_price
        taxes = tmpl.taxes_id.filtered(lambda t: t.company_id == self.env.company) \
            or tmpl.taxes_id
        if taxes:
            price = taxes.compute_all(
                tmpl.list_price, self.env.company.currency_id, 1.0, product=None, partner=None
            )["total_included"]
        return price

    # ------------------------------------------------------------------
    # Workbook
    # ------------------------------------------------------------------
    def _store_label(self, location):
        name = location.complete_name or location.display_name or ""
        marker = "Physical Locations/"
        if marker in name:
            name = name.split(marker, 1)[1]
        return name

    def _build_workbook(self, stores, rows):
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("Store Product Quant Sheet")

        header_fmt = workbook.add_format({
            "bold": 1, "border": 1, "bg_color": "#AAB7B8",
            "font_size": 10, "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        cell_fmt = workbook.add_format({
            "font_name": "KacstBook", "font_size": 10, "align": "center",
            "valign": "vcenter", "text_wrap": True, "border": 1,
        })
        qty_fmt = workbook.add_format({
            "font_name": "KacstBook", "font_size": 10, "align": "center",
            "valign": "vcenter", "border": 1, "num_format": "#,##0.###",
        })
        price_fmt = workbook.add_format({
            "font_name": "KacstBook", "font_size": 10, "align": "center",
            "valign": "vcenter", "border": 1, "num_format": "#,##0.00",
        })

        # Fixed columns.
        sheet.set_column(0, 1, 20)
        sheet.set_column(2, 2, 80)
        sheet.set_column(3, 5, 18)
        headers = ["Product Category", "Product Family", "Description",
                   "Price", "Total QTY", "Total QTY Sold"]
        for col, label in enumerate(headers):
            sheet.write(0, col, label, header_fmt)

        # Per-store columns: [On Hand, Sold] pairs (legacy layout).
        base = len(headers)
        store_cols = {}  # loc_id -> on-hand column index
        for i, loc in enumerate(stores):
            onhand_col = base + i * 2
            sold_col = onhand_col + 1
            store_cols[loc.id] = onhand_col
            label = self._store_label(loc)
            sheet.set_column(onhand_col, sold_col, 16)
            sheet.write(0, onhand_col, label, header_fmt)
            sheet.write(0, sold_col, "%s Sold" % label, header_fmt)

        # Rows.
        row = 1
        for data in rows:
            sheet.write(row, 0, data["category"], cell_fmt)
            sheet.write(row, 1, data["family"], cell_fmt)
            sheet.write(row, 2, data["description"], cell_fmt)
            sheet.write_number(row, 3, data["price"], price_fmt)
            sheet.write_number(row, 4, data["total_qty"], qty_fmt)
            sheet.write_number(row, 5, data["total_sold"], qty_fmt)
            for loc in stores:
                onhand_col = store_cols[loc.id]
                sheet.write_number(row, onhand_col, data["onhand"][loc.id], qty_fmt)
                sheet.write_number(row, onhand_col + 1, data["sold"][loc.id], qty_fmt)
            row += 1

        last_col = base + len(stores) * 2 - 1
        sheet.freeze_panes(1, 3)
        if row > 1:
            sheet.autofilter(0, 0, row - 1, last_col)

        workbook.close()
        output.seek(0)
        return output.getvalue()

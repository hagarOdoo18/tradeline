# -*- coding: utf-8 -*-
import base64
from collections import defaultdict
from io import BytesIO

import xlsxwriter

from odoo import _, fields, models
from odoo.exceptions import UserError

VENDOR_GROUP = "tradeline_store_quant_excel.group_store_quant_vendor"
_PHYS = "Physical Locations/"


class StoreReportWizard(models.TransientModel):
    _name = "store.report.wizard"
    _description = "Store Product Quantity Excel Wizard"

    gentextfile = fields.Binary(
        string="Click On Save As Button To Download File", readonly=True)
    filename = fields.Char(string="File name", readonly=True)

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------
    def action_generate_warranty_report(self):
        self.ensure_one()

        stores = self._get_store_locations()
        if not stores:
            raise UserError(_("No active internal locations were found."))

        onhand, total, variant_ids = self._collect_stock(stores)
        if not variant_ids:
            raise UserError(_("No products with stock on hand were found."))

        products = self.env["product.product"].browse(sorted(variant_ids)).sorted(
            key=lambda p: (
                (p.product_tmpl_id.categ_id.name or "").lower(),
                (p.product_tmpl_id.family_id.name or "").lower(),
                (p.display_name or "").lower(),
                (p.barcode or ""),
            ))

        content = self._build_workbook(stores, products, onhand, total)
        filename = "Store Product Quant Sheet.xlsx"
        self.write({"gentextfile": base64.b64encode(content), "filename": filename})

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
        """Active internal stock locations = the report's store columns."""
        Location = self.env["stock.location"]
        locations = Location.search([("usage", "=", "internal"), ("active", "=", True)])
        # Legacy ordered by a custom 'location_number' that may no longer exist.
        if "location_number" in Location._fields:
            return locations.sorted(
                key=lambda l: (l.location_number or 0, l.complete_name or ""))
        return locations.sorted(key=lambda l: l.complete_name or "")

    def _collect_stock(self, stores):
        """On-hand qty per (variant, location) in ONE aggregate query, replacing
        the legacy per-product/per-store SQL loop. Keeps only variants whose
        total on-hand across the store locations is non-zero (legacy filter)."""
        store_ids = stores.ids
        onhand = defaultdict(float)   # (variant_id, loc_id) -> qty
        total = defaultdict(float)    # variant_id -> qty
        groups = self.env["stock.quant"]._read_group(
            [("location_id", "in", store_ids), ("product_id.active", "=", True)],
            groupby=["product_id", "location_id"],
            aggregates=["quantity:sum"],
        )
        for product, location, qty in groups:
            if not qty:
                continue
            onhand[(product.id, location.id)] += qty
            total[product.id] += qty
        variant_ids = {vid for vid, qty in total.items() if qty}
        return onhand, total, variant_ids

    def _vendor_name(self, tmpl):
        seller = tmpl.seller_ids[:1]
        return seller.partner_id.name if seller and seller.partner_id else ""

    def _price_with_taxes(self, tmpl):
        """Tax-inclusive price (replaces the legacy product_template.price_with_taxes)."""
        price = tmpl.list_price
        taxes = tmpl.taxes_id.filtered(lambda t: t.company_id == self.env.company) \
            or tmpl.taxes_id
        if taxes:
            price = taxes.compute_all(
                tmpl.list_price, self.env.company.currency_id, 1.0,
                product=None, partner=None)["total_included"]
        return price

    def _store_label(self, location):
        name = location.complete_name or location.display_name or ""
        if _PHYS in name:
            name = name.split(_PHYS, 1)[1]
        return name

    # ------------------------------------------------------------------
    # Workbook
    # ------------------------------------------------------------------
    def _build_workbook(self, stores, products, onhand, total):
        with_vendor = self.env.user.has_group(VENDOR_GROUP)

        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("Store Product Quant Sheet")

        header_fmt = workbook.add_format({
            "bold": 1, "border": 1, "bg_color": "#AAB7B8", "font_size": 10,
            "align": "center", "valign": "vcenter", "text_wrap": True,
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

        if with_vendor:
            headers = ["Product Category", "Product Family", "Sub Category",
                       "Vendor Name", "Description", "Item Code", "Price", "Total QTY"]
            sheet.set_column(0, 3, 20)
            sheet.set_column(4, 4, 80)
            sheet.set_column(5, 7, 18)
        else:
            headers = ["Product Category", "Product Family", "Item Code",
                       "Description", "Price", "Total QTY"]
            sheet.set_column(0, 2, 20)
            sheet.set_column(3, 3, 80)
            sheet.set_column(4, 5, 18)
        for col, label in enumerate(headers):
            sheet.write(0, col, label, header_fmt)

        base = len(headers)
        store_col = {}
        for i, loc in enumerate(stores):
            c = base + i
            store_col[loc.id] = c
            sheet.set_column(c, c, 18)
            sheet.write(0, c, self._store_label(loc), header_fmt)

        row = 1
        for product in products:
            tmpl = product.product_tmpl_id
            if with_vendor:
                sheet.write(row, 0, tmpl.categ_id.name or "", cell_fmt)
                sheet.write(row, 1, tmpl.family_id.name or "", cell_fmt)
                sheet.write(row, 2, tmpl.sub_categ_id.name or "", cell_fmt)
                sheet.write(row, 3, self._vendor_name(tmpl), cell_fmt)
                sheet.write(row, 4, product.display_name or tmpl.name or "", cell_fmt)
                sheet.write(row, 5, product.barcode or "", cell_fmt)
                sheet.write_number(row, 6, self._price_with_taxes(tmpl), price_fmt)
                sheet.write_number(row, 7, total.get(product.id, 0.0), qty_fmt)
            else:
                sheet.write(row, 0, tmpl.categ_id.name or "", cell_fmt)
                sheet.write(row, 1, tmpl.family_id.name or "", cell_fmt)
                sheet.write(row, 2, product.barcode or "", cell_fmt)
                sheet.write(row, 3, product.display_name or tmpl.name or "", cell_fmt)
                sheet.write_number(row, 4, self._price_with_taxes(tmpl), price_fmt)
                sheet.write_number(row, 5, total.get(product.id, 0.0), qty_fmt)
            for loc in stores:
                qty = onhand.get((product.id, loc.id), 0.0)
                if qty:
                    sheet.write_number(row, store_col[loc.id], qty, qty_fmt)
                else:
                    sheet.write(row, store_col[loc.id], "", cell_fmt)
            row += 1

        last_col = base + max(len(stores), 1) - 1
        sheet.freeze_panes(1, base)
        if row > 1:
            sheet.autofilter(0, 0, row - 1, last_col)

        workbook.close()
        output.seek(0)
        return output.getvalue()

# -*- coding: utf-8 -*-
import base64
from collections import defaultdict
from datetime import datetime, time, timedelta
from io import BytesIO

import xlsxwriter

from odoo import _, fields, models
from odoo.exceptions import UserError

# Customer invoice move types: net sold = out_invoice - out_refund.
INVOICE_SIGNS = (("out_invoice", 1.0), ("out_refund", -1.0))
SRO_INV_TYPE = "sro"
SALE_STATE = "sale"


class StoreReportQtySoldWizard(models.TransientModel):
    _name = "store.report.qty.sold.wizard"
    _description = "Store Product Quantity Sold Excel Wizard"

    date_from = fields.Date(string="From",
                            default=lambda self: fields.Date.today() - timedelta(days=30))
    date_to = fields.Date(string="To", default=lambda self: fields.Date.today())
    gentextfile = fields.Binary(string="Report", readonly=True)
    filename = fields.Char(string="File name", readonly=True)

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------
    def action_generate_report(self):
        self.ensure_one()
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError(_("'From' must be before 'To'."))

        sold, total_sold = self._collect_sold_data()
        branch_ids = {bid for (_pid, bid) in sold if bid}
        if not total_sold:
            raise UserError(_("No sold quantities were found for the selected period."))

        branches = self.env["res.branch"].browse(sorted(branch_ids)).sorted(
            key=lambda b: (b.name or "").lower())
        products = self.env["product.product"].browse(sorted(total_sold)).sorted(
            key=lambda p: (
                (p.product_tmpl_id.categ_id.name or "").lower(),
                (p.product_tmpl_id.family_id.name or "").lower(),
                (p.display_name or "").lower(),
            ))

        content = self._build_workbook(branches, sold, total_sold, products)
        filename = "Store Product Quant Sold Sheet.xlsx"
        self.write({"gentextfile": base64.b64encode(content), "filename": filename})

        return {
            "type": "ir.actions.act_url",
            "name": "Store Product Quant Sold Sheet",
            "url": "/web/content/%s/%s/gentextfile/%s?download=true"
                   % (self._name, self.id, filename),
            "target": "new",
        }

    # ------------------------------------------------------------------
    # Data gathering -- aggregated in the database, keyed by branch
    # ------------------------------------------------------------------
    def _collect_sold_data(self):
        """Sold qty per (variant, branch). Invoices: two grouped queries
        (branch_id + product_id both stored on account.move.line). SRO orders:
        one grouped query per branch. Independent of the number of lines in the
        period, so wide date ranges no longer load every line into memory."""
        sold = defaultdict(float)        # (variant_id, branch_id) -> qty
        total_sold = defaultdict(float)  # variant_id -> qty

        AML = self.env["account.move.line"]
        SOL = self.env["sale.order.line"]
        df, dt = self.date_from, self.date_to

        for move_type, sign in INVOICE_SIGNS:
            inv_domain = [
                ("display_type", "=", "product"),
                ("product_id", "!=", False),
                ("move_id.state", "=", "posted"),
                ("move_id.move_type", "=", move_type),
            ]
            if df:
                inv_domain.append(("move_id.invoice_date", ">=", df))
            if dt:
                inv_domain.append(("move_id.invoice_date", "<=", dt))
            for branch, product, qty in AML._read_group(
                    inv_domain, groupby=["branch_id", "product_id"],
                    aggregates=["quantity:sum"]):
                if not qty:
                    continue
                bid = branch.id if branch else 0
                sold[(product.id, bid)] += qty * sign
                total_sold[product.id] += qty * sign

        dt_from = datetime.combine(df, time.min) if df else None
        dt_to = datetime.combine(dt, time.max) if dt else None
        for branch in self.env["res.branch"].search([]):
            so_domain = [
                ("display_type", "=", False),
                ("product_id", "!=", False),
                ("order_id.inv_type", "=", SRO_INV_TYPE),
                ("order_id.state", "=", SALE_STATE),
                ("order_id.branch_id", "=", branch.id),
            ]
            if dt_from:
                so_domain.append(("order_id.date_order", ">=", dt_from))
            if dt_to:
                so_domain.append(("order_id.date_order", "<=", dt_to))
            for product, qty in SOL._read_group(
                    so_domain, groupby=["product_id"],
                    aggregates=["qty_delivered:sum"]):
                if not qty:
                    continue
                sold[(product.id, branch.id)] += qty
                total_sold[product.id] += qty

        return sold, total_sold

    def _vendor_name(self, tmpl):
        seller = tmpl.seller_ids[:1]
        return seller.partner_id.name if seller and seller.partner_id else ""

    def _price_with_taxes(self, tmpl):
        price = tmpl.list_price
        taxes = tmpl.taxes_id.filtered(lambda t: t.company_id == self.env.company) \
            or tmpl.taxes_id
        if taxes:
            price = taxes.compute_all(
                tmpl.list_price, self.env.company.currency_id, 1.0,
                product=None, partner=None)["total_included"]
        return price

    # ------------------------------------------------------------------
    # Workbook
    # ------------------------------------------------------------------
    def _build_workbook(self, branches, sold, total_sold, products):
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("Store Product Quant Sold Sheet")

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

        headers = ["Product Category", "Product Family", "Vendor Name",
                   "Item Code", "Description", "Price", "Total QTY Sold"]
        sheet.set_column(0, 3, 20)
        sheet.set_column(4, 4, 80)
        sheet.set_column(5, 6, 18)
        for col, label in enumerate(headers):
            sheet.write(0, col, label, header_fmt)

        base = len(headers)
        branch_col = {}
        for i, branch in enumerate(branches):
            col = base + i
            branch_col[branch.id] = col
            sheet.set_column(col, col, 20)
            sheet.write(0, col, branch.name or "", header_fmt)

        row = 1
        for product in products:
            tmpl = product.product_tmpl_id
            sheet.write(row, 0, tmpl.categ_id.name or "", cell_fmt)
            sheet.write(row, 1, tmpl.family_id.name or "", cell_fmt)
            sheet.write(row, 2, self._vendor_name(tmpl), cell_fmt)
            sheet.write(row, 3, product.barcode or "", cell_fmt)
            sheet.write(row, 4, product.display_name or tmpl.name or "", cell_fmt)
            sheet.write_number(row, 5, self._price_with_taxes(tmpl), price_fmt)
            sheet.write_number(row, 6, total_sold.get(product.id, 0.0), qty_fmt)
            for branch in branches:
                sheet.write_number(row, branch_col[branch.id],
                                   sold.get((product.id, branch.id), 0.0), qty_fmt)
            row += 1

        last_col = base + max(len(branches), 1) - 1
        sheet.freeze_panes(1, 5)
        if row > 1:
            sheet.autofilter(0, 0, row - 1, last_col)

        workbook.close()
        output.seek(0)
        return output.getvalue()

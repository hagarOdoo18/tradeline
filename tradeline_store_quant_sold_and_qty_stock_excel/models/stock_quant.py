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
# SRO sale orders counted on top of invoices.
SRO_INV_TYPE = "sro"
SALE_STATE = "sale"


class StoreReportQtySoldStockWizard(models.TransientModel):
    _name = "store.report.qty.sold.stock.wizard"
    _description = "Store Product Quantity Sold and Stock QTY Excel Wizard"

    date_from = fields.Date(
        string="From", required=True,
        default=lambda self: fields.Date.today() - timedelta(days=30))
    date_to = fields.Date(
        string="To", required=True,
        default=lambda self: fields.Date.today())
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
        stock_by_prod_branch, total_stock = self._collect_stock_data()

        # Store columns = branches that sold in the period OR hold stock.
        branch_ids = {bid for (_pid, bid) in sold if bid}
        branch_ids |= {bid for (_pid, bid) in stock_by_prod_branch if bid}
        branches = self.env["res.branch"].browse(sorted(branch_ids)).sorted(
            key=lambda b: (b.name or "").lower())

        # Rows = everything sold, plus anything on hand in a shown branch.
        shown_ids = set(branches.ids)
        prod_ids = set(total_sold)
        for (pid, bid), qty in stock_by_prod_branch.items():
            if qty and bid in shown_ids:
                prod_ids.add(pid)
        if not prod_ids:
            raise UserError(_("No data found for the selected period."))

        products = self.env["product.product"].browse(sorted(prod_ids)).sorted(
            key=lambda p: (
                (p.product_tmpl_id.categ_id.name or "").lower(),
                (p.product_tmpl_id.family_id.name or "").lower(),
                (p.display_name or "").lower(),
            ))

        content = self._build_workbook(
            branches, sold, total_sold, stock_by_prod_branch, total_stock, products)
        filename = "Store Product Quant Sold and Stock QTY Sheet.xlsx"
        self.write({"gentextfile": base64.b64encode(content), "filename": filename})

        return {
            "type": "ir.actions.act_url",
            "name": "Store Product Quant Sold and Stock QTY Sheet",
            "url": "/web/content/%s/%s/gentextfile/%s?download=true"
                   % (self._name, self.id, filename),
            "target": "new",
        }

    # ------------------------------------------------------------------
    # Sold data -- aggregated in the database, keyed by branch
    # ------------------------------------------------------------------
    def _collect_sold_data(self):
        """Sold qty per (variant, branch). Invoices are summed in two grouped
        queries (branch_id + product_id are both stored on account.move.line);
        SRO orders in one grouped query per branch. Cost is bounded by the
        number of branches, never by the number of lines in the date range."""
        sold = defaultdict(float)        # (variant_id, branch_id) -> qty
        total_sold = defaultdict(float)  # variant_id -> qty

        AML = self.env["account.move.line"]
        SOL = self.env["sale.order.line"]
        df, dt = self.date_from, self.date_to

        # --- Posted customer invoices, net of refunds (2 queries) --------
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
                    inv_domain,
                    groupby=["branch_id", "product_id"],
                    aggregates=["quantity:sum"]):
                if not qty:
                    continue
                bid = branch.id if branch else 0
                sold[(product.id, bid)] += qty * sign
                total_sold[product.id] += qty * sign

        # --- Confirmed SRO sale orders, delivered qty (per branch) -------
        dt_from = datetime.combine(df, time.min) if df else None
        dt_to = datetime.combine(dt, time.max) if dt else None
        branches = self.env["res.branch"].search([])
        for branch in branches:
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

    # ------------------------------------------------------------------
    # Stock data -- one aggregate query, keyed by branch
    # ------------------------------------------------------------------
    def _collect_stock_data(self):
        stock = defaultdict(float)        # (variant_id, branch_id) -> qty
        total_stock = defaultdict(float)  # variant_id -> qty
        for branch, product, qty in self.env["stock.quant"]._read_group(
                [
                    ("location_id.usage", "=", "internal"),
                    ("location_id.active", "=", True),
                    ("product_id.active", "=", True),
                ],
                groupby=["branch_id", "product_id"],
                aggregates=["quantity:sum"]):
            if not qty:
                continue
            bid = branch.id if branch else 0
            stock[(product.id, bid)] += qty
            total_stock[product.id] += qty
        return stock, total_stock

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Workbook
    # ------------------------------------------------------------------
    def _build_workbook(self, branches, sold, total_sold,
                        stock, total_stock, products):
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("Sold & Stock QTY")

        header_fmt = workbook.add_format({
            "bold": 1, "border": 1, "bg_color": "#AAB7B8", "font_size": 10,
            "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        store_sub_fmt = workbook.add_format({
            "bold": 1, "border": 1, "bg_color": "#FF0000", "font_color": "#FFFFFF",
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

        fixed = ["Product Category", "Product Family", "Item Code", "Description",
                 "Price", "Total Stock QTY", "Total QTY Sold"]
        for col, label in enumerate(fixed):
            sheet.merge_range(0, col, 1, col, label, header_fmt)
        sheet.set_column(0, 1, 20)
        sheet.set_column(2, 2, 18)
        sheet.set_column(3, 3, 80)
        sheet.set_column(4, 6, 16)

        base = len(fixed)
        branch_sold_col = {}
        branch_stock_col = {}
        for i, branch in enumerate(branches):
            c = base + i * 2
            sheet.merge_range(0, c, 0, c + 1, branch.name or "", header_fmt)
            sheet.write(1, c, "Sold QTY", store_sub_fmt)
            sheet.write(1, c + 1, "Stock QTY", store_sub_fmt)
            sheet.set_column(c, c + 1, 16)
            branch_sold_col[branch.id] = c
            branch_stock_col[branch.id] = c + 1

        row = 2
        for product in products:
            tmpl = product.product_tmpl_id
            sheet.write(row, 0, tmpl.categ_id.name or "", cell_fmt)
            sheet.write(row, 1, tmpl.family_id.name or "", cell_fmt)
            sheet.write(row, 2, product.barcode or "", cell_fmt)
            sheet.write(row, 3, product.display_name or tmpl.name or "", cell_fmt)
            sheet.write_number(row, 4, self._price_with_taxes(tmpl), price_fmt)
            sheet.write_number(row, 5, total_stock.get(product.id, 0.0), qty_fmt)
            sheet.write_number(row, 6, total_sold.get(product.id, 0.0), qty_fmt)
            for branch in branches:
                sq = sold.get((product.id, branch.id), 0.0)
                stk = stock.get((product.id, branch.id), 0.0)
                sheet.write_number(row, branch_sold_col[branch.id], sq, qty_fmt)
                if stk:
                    sheet.write_number(row, branch_stock_col[branch.id], stk, qty_fmt)
                else:
                    sheet.write(row, branch_stock_col[branch.id], "", cell_fmt)
            row += 1

        last_col = base + max(len(branches) * 2, 1) - 1
        sheet.freeze_panes(2, 4)
        if row > 2:
            sheet.autofilter(1, 0, row - 1, last_col)

        workbook.close()
        output.seek(0)
        return output.getvalue()

# -*- coding: utf-8 -*-
import base64
from collections import defaultdict
from datetime import datetime, time, timedelta
from io import BytesIO

import xlsxwriter

from odoo import _, fields, models
from odoo.exceptions import UserError

# Customer invoice move types (net sold = out_invoice - out_refund).
INVOICE_TYPES = ("out_invoice", "out_refund")
# Sale orders counted: SRO type, confirmed.
SRO_INV_TYPE = "sro"
SALE_STATE = "sale"
# Sales team excluded from the columns (matches the legacy name filter).
EXCLUDED_TEAM = "sales"


class StoreReportQtySoldWizard(models.TransientModel):
    _name = "store.report.qty.sold.wizard"
    _description = "Store Product Quantity Sold Excel Wizard"

    date_from = fields.Date(string="From", default=lambda self: fields.Date.today() - timedelta(days=30))
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

        teams, sold, total_sold, products = self._collect_sold_data()
        if not products:
            raise UserError(_("No sold quantities were found for the selected period."))

        content = self._build_workbook(teams, sold, total_sold, products)
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
    # Data gathering (ORM) -- sold qty per (template, sales team)
    # ------------------------------------------------------------------
    def _date_bounds(self):
        dt_from = datetime.combine(self.date_from, time.min) if self.date_from else None
        dt_to = datetime.combine(self.date_to, time.max) if self.date_to else None
        return dt_from, dt_to

    @staticmethod
    def _is_excluded(team):
        return bool(team) and (team.name or "").strip().lower() == EXCLUDED_TEAM

    def _collect_sold_data(self):
        sold = defaultdict(float)        # (variant_id, team_id) -> qty
        total_sold = defaultdict(float)  # variant_id -> qty
        teams = {}                       # team_id -> team record (excl. 'sales')
        variant_ids = set()

        def add(product, team, qty):
            # Keyed on the product *variant*: the item code (barcode) lives on
            # product.product, and a template may carry several barcoded variants.
            if not qty:
                return
            team_id = team.id if team else 0
            sold[(product.id, team_id)] += qty
            total_sold[product.id] += qty
            variant_ids.add(product.id)
            if team and team_id not in teams:
                teams[team_id] = team

        # --- Invoices: posted customer moves, net of refunds -------------
        inv_domain = [
            ("state", "=", "posted"),
            ("move_type", "in", list(INVOICE_TYPES)),
        ]
        dt_from, dt_to = self.date_from, self.date_to
        if dt_from:
            inv_domain.append(("invoice_date", ">=", dt_from))
        if dt_to:
            inv_domain.append(("invoice_date", "<=", dt_to))
        # One search; related reads (team_id, invoice_line_ids) are prefetched
        # in bulk by the ORM -- no per-product/per-store query like the legacy SQL.
        moves = self.env["account.move"].search(inv_domain)
        for move in moves:
            team = move.team_id
            if self._is_excluded(team):
                continue
            sign = 1.0 if move.move_type == "out_invoice" else -1.0
            for line in move.invoice_line_ids:
                if line.display_type or not line.product_id:
                    continue
                add(line.product_id, team, line.quantity * sign)

        # --- SRO sale orders: confirmed, delivered qty -------------------
        so_domain = [("inv_type", "=", SRO_INV_TYPE), ("state", "=", SALE_STATE)]
        dt_from, dt_to = self._date_bounds()
        if dt_from:
            so_domain.append(("date_order", ">=", dt_from))
        if dt_to:
            so_domain.append(("date_order", "<=", dt_to))
        orders = self.env["sale.order"].search(so_domain)
        for order in orders:
            team = order.team_id
            if self._is_excluded(team):
                continue
            for line in order.order_line:
                if line.display_type or not line.product_id:
                    continue
                add(line.product_id, team, line.qty_delivered)

        products = self.env["product.product"].browse(sorted(variant_ids))
        ordered_teams = sorted(teams.values(), key=lambda t: (t.name or "").lower())
        return ordered_teams, sold, total_sold, products

    def _vendor_name(self, tmpl):
        seller = tmpl.seller_ids[:1]
        return seller.partner_id.name if seller and seller.partner_id else ""

    def _price_with_taxes(self, tmpl):
        """Tax-inclusive price (replaces the legacy price_with_taxes column)."""
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
    def _build_workbook(self, teams, sold, total_sold, products):
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
        team_col = {}
        for i, team in enumerate(teams):
            col = base + i
            team_col[team.id] = col
            sheet.set_column(col, col, 20)
            sheet.write(0, col, team.name or "", header_fmt)

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
            for team in teams:
                qty = sold.get((product.id, team.id), 0.0)
                sheet.write_number(row, team_col[team.id], qty, qty_fmt)
            row += 1

        last_col = base + len(teams) - 1
        sheet.freeze_panes(1, 5)
        if row > 1:
            sheet.autofilter(0, 0, row - 1, last_col)

        workbook.close()
        output.seek(0)
        return output.getvalue()

# -*- coding: utf-8 -*-
import base64
from collections import defaultdict
from datetime import datetime, time, timedelta
from io import BytesIO

import xlsxwriter

from odoo import _, fields, models
from odoo.exceptions import UserError

# Customer invoice move types: net sold = out_invoice - out_refund.
INVOICE_TYPES = ("out_invoice", "out_refund")
# SRO sale orders counted on top of invoices.
SRO_INV_TYPE = "sro"
SALE_STATE = "sale"
# Sales team excluded from the store columns (matches the legacy name filter).
EXCLUDED_TEAM = "sales"


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

        sold, total_sold, sold_team_ids = self._collect_sold_data()
        stock_by_prod_loc, total_stock = self._collect_stock_data()
        teams = self._get_store_teams(sold_team_ids)

        store_loc_ids = set()
        team_locs = {}
        for team in teams:
            loc_ids = self._team_internal_locations(team).ids
            team_locs[team.id] = loc_ids
            store_loc_ids.update(loc_ids)

        # Rows: everything sold in the period, plus anything on hand at a store
        # location even if it was not sold (mirrors the legacy "not sold" pass).
        prod_ids = set(total_sold)
        for (pid, lid), qty in stock_by_prod_loc.items():
            if qty and lid in store_loc_ids:
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
            teams, team_locs, sold, total_sold, stock_by_prod_loc, total_stock, products)
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
    # Data gathering
    # ------------------------------------------------------------------
    def _collect_sold_data(self):
        """Sold qty per (variant, team). One search each for invoices / SRO
        orders; related reads are bulk-prefetched by the ORM (no per-row SQL)."""
        sold = defaultdict(float)        # (variant_id, team_id) -> qty
        total_sold = defaultdict(float)  # variant_id -> qty
        team_ids = set()

        def add(product, team, qty):
            if not qty or not product:
                return
            if team and (team.name or "").strip().lower() == EXCLUDED_TEAM:
                return
            tid = team.id if team else 0
            sold[(product.id, tid)] += qty
            total_sold[product.id] += qty
            if tid:
                team_ids.add(tid)

        # --- Posted customer invoices, net of refunds --------------------
        inv_domain = [
            ("state", "=", "posted"),
            ("move_type", "in", list(INVOICE_TYPES)),
        ]
        if self.date_from:
            inv_domain.append(("invoice_date", ">=", self.date_from))
        if self.date_to:
            inv_domain.append(("invoice_date", "<=", self.date_to))
        for move in self.env["account.move"].search(inv_domain):
            sign = 1.0 if move.move_type == "out_invoice" else -1.0
            team = move.team_id
            for line in move.invoice_line_ids:
                if line.display_type or not line.product_id:
                    continue
                add(line.product_id, team, line.quantity * sign)

        # --- Confirmed SRO sale orders, delivered qty --------------------
        dt_from = datetime.combine(self.date_from, time.min) if self.date_from else None
        dt_to = datetime.combine(self.date_to, time.max) if self.date_to else None
        so_domain = [("inv_type", "=", SRO_INV_TYPE), ("state", "=", SALE_STATE)]
        if dt_from:
            so_domain.append(("date_order", ">=", dt_from))
        if dt_to:
            so_domain.append(("date_order", "<=", dt_to))
        for order in self.env["sale.order"].search(so_domain):
            team = order.team_id
            for line in order.order_line:
                if line.display_type or not line.product_id:
                    continue
                add(line.product_id, team, line.qty_delivered)

        return sold, total_sold, team_ids

    def _collect_stock_data(self):
        """On-hand qty per (variant, location) in ONE aggregate query, replacing
        the legacy per-product/per-location SQL loop."""
        stock_by_prod_loc = defaultdict(float)
        total_stock = defaultdict(float)
        groups = self.env["stock.quant"]._read_group(
            [
                ("location_id.usage", "=", "internal"),
                ("location_id.active", "=", True),
                ("product_id.active", "=", True),
            ],
            groupby=["product_id", "location_id"],
            aggregates=["quantity:sum"],
        )
        for product, location, qty in groups:
            if not qty:
                continue
            stock_by_prod_loc[(product.id, location.id)] += qty
            total_stock[product.id] += qty
        return stock_by_prod_loc, total_stock

    def _team_internal_locations(self, team):
        """Internal stock locations mapped to a sales team (via crm.team.location_ids,
        the same field the legacy report used). Guarded so the module still
        installs if that field is absent -- stock columns simply stay empty."""
        Team = self.env["crm.team"]
        if "location_ids" not in Team._fields or not team:
            return self.env["stock.location"]
        return team.location_ids.filtered(
            lambda loc: loc.usage == "internal" and loc.active)

    def _get_store_teams(self, sold_team_ids):
        """Columns = teams that sold in the period, plus teams with a stock
        location, excluding the generic 'sales' team; ordered by name."""
        Team = self.env["crm.team"]
        teams = Team.browse(sorted(tid for tid in sold_team_ids if tid))
        if "location_ids" in Team._fields:
            teams |= Team.search([("location_ids", "!=", False)])
        teams = teams.filtered(
            lambda t: (t.name or "").strip().lower() != EXCLUDED_TEAM)
        return teams.sorted(key=lambda t: (t.name or "").lower())

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
    def _build_workbook(self, teams, team_locs, sold, total_sold,
                        stock_by_prod_loc, total_stock, products):
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
        team_sold_col = {}
        team_stock_col = {}
        for i, team in enumerate(teams):
            c = base + i * 2
            sheet.merge_range(0, c, 0, c + 1, team.name or "", header_fmt)
            sheet.write(1, c, "Sold QTY", store_sub_fmt)
            sheet.write(1, c + 1, "Stock QTY", store_sub_fmt)
            sheet.set_column(c, c + 1, 16)
            team_sold_col[team.id] = c
            team_stock_col[team.id] = c + 1

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
            for team in teams:
                sq = sold.get((product.id, team.id), 0.0)
                stk = sum(stock_by_prod_loc.get((product.id, lid), 0.0)
                          for lid in team_locs.get(team.id, ()))
                sheet.write_number(row, team_sold_col[team.id], sq, qty_fmt)
                if stk:
                    sheet.write_number(row, team_stock_col[team.id], stk, qty_fmt)
                else:
                    sheet.write(row, team_stock_col[team.id], "", cell_fmt)
            row += 1

        last_col = base + max(len(teams) * 2, 1) - 1
        sheet.freeze_panes(2, 4)
        if row > 2:
            sheet.autofilter(1, 0, row - 1, last_col)

        workbook.close()
        output.seek(0)
        return output.getvalue()

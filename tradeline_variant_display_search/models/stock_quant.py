# -*- coding: utf-8 -*-
from odoo import fields, models


class StockQuant(models.Model):
    _inherit = "stock.quant"

    product_search_text = fields.Char(
        string="Product",
        compute="_compute_product_search_text",
        search="_search_product_search_text",
    )

    def _compute_product_search_text(self):
        for quant in self:
            quant.product_search_text = quant.product_id.display_name or ""

    def _search_product_search_text(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []
        return [("product_id.display_name", "ilike", value)]


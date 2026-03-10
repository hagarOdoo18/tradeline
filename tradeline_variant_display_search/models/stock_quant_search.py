# -*- coding: utf-8 -*-
from odoo import api, fields, models


class StockQuant(models.Model):
    _inherit = "stock.quant"

    product_search_text = fields.Char(
        string="Product",
        compute="_compute_product_search_text",
        search="_search_product_search_text",
        store=False,
    )

    @api.depends("product_id")
    def _compute_product_search_text(self):
        for quant in self:
            quant.product_search_text = quant.product_id.display_name or ""

    def _search_product_search_text(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []

        product_matches = self.env["product.product"].name_search(
            name=value,
            operator=operator,
            limit=5000,
        )
        product_ids = [product_id for product_id, _name in product_matches]
        if not product_ids:
            return [("id", "=", 0)]
        return [("product_id", "in", product_ids)]

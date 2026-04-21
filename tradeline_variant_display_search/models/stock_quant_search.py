# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.osv import expression

from .product_search import SUPPORTED_TEXT_OPERATORS


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

        effective_operator = operator if operator in SUPPORTED_TEXT_OPERATORS else "ilike"
        products = self.env["product.product"].search(
            expression.OR(
                [
                    [("display_name", effective_operator, value)],
                    [("name", effective_operator, value)],
                    [("product_tmpl_id.name", effective_operator, value)],
                    [("product_template_variant_value_ids.name", effective_operator, value)],
                    [("barcode", effective_operator, value)],
                    [("default_code", effective_operator, value)],
                ]
            ),
            limit=5000,
        )
        product_ids = products.ids
        if not product_ids:
            return [("id", "=", 0)]
        return [("product_id", "in", product_ids)]

    @api.model
    def _rewrite_product_id_text_domain(self, domain):
        """Map product_id text searches to explicit product ids via name_search.

        This makes Enter behavior consistent across stock.quant actions/search views,
        not only views that include product_search_text.
        """
        if not domain:
            return domain

        def _rewrite_leaf(term):
            if not isinstance(term, (list, tuple)) or len(term) < 3:
                return term
            field_name, operator, value = term[0], term[1], term[2]
            if field_name != "product_id":
                return term
            if operator not in SUPPORTED_TEXT_OPERATORS:
                return term
            if not isinstance(value, str):
                return term
            value = value.strip()
            if not value:
                return term

            product_matches = self.env["product.product"].name_search(
                name=value,
                operator=operator,
                limit=5000,
            )
            product_ids = [product_id for product_id, _name in product_matches]
            if not product_ids:
                return ("id", "=", 0)
            return ("product_id", "in", product_ids)

        def _rewrite(node):
            if isinstance(node, tuple):
                return _rewrite_leaf(node)
            if isinstance(node, list):
                return [_rewrite(item) for item in node]
            return node

        return _rewrite(domain)

    @api.model
    def search(self, domain, offset=0, limit=None, order=None):
        domain = self._rewrite_product_id_text_domain(domain)
        return super().search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        domain = self._rewrite_product_id_text_domain(domain)
        return super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )

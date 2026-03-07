# -*- coding: utf-8 -*-
from odoo import api, models
from odoo.osv import expression


SUPPORTED_TEXT_OPERATORS = {"ilike", "like", "=ilike", "=like"}


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model
    def _name_search(self, name, domain=None, operator="ilike", limit=None, order=None):
        domain = list(domain or [])
        result = super()._name_search(
            name=name,
            domain=domain,
            operator=operator,
            limit=limit,
            order=order,
        )
        if result or not name or operator not in SUPPORTED_TEXT_OPERATORS:
            return result

        lot_product = self.env["stock.lot"].search([("name", "=", name)], limit=1).product_id
        product_domains = [
            [("display_name", operator, name)],
            [("name", operator, name)],
            [("barcode", operator, name)],
            [("default_code", operator, name)],
            [("product_template_variant_value_ids.name", operator, name)],
        ]
        if lot_product:
            product_domains.append([("id", "=", lot_product.id)])

        product_ids = self.env["product.product"]._search(
            expression.OR(product_domains),
            limit=limit,
        )
        if not product_ids:
            return result

        template_domain = expression.AND([domain, [("product_variant_ids", "in", product_ids)]])
        return self._search(template_domain, limit=limit, order=order)


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        args = list(args or [])
        result = super().name_search(name=name, args=args, operator=operator, limit=limit)
        if result or not name or operator not in SUPPORTED_TEXT_OPERATORS:
            return result

        lot_product = self.env["stock.lot"].search([("name", "=", name)], limit=1).product_id
        product_domains = [
            [("display_name", operator, name)],
            [("name", operator, name)],
            [("barcode", operator, name)],
            [("default_code", operator, name)],
            [("product_template_variant_value_ids.name", operator, name)],
        ]
        if lot_product:
            product_domains.append([("id", "=", lot_product.id)])

        fallback_domain = expression.AND([args, expression.OR(product_domains)])
        products = self.search(fallback_domain, limit=limit)
        return [(product.id, product.display_name) for product in products]


# -*- coding: utf-8 -*-
import re

from odoo import api, models
from odoo.osv import expression


SUPPORTED_TEXT_OPERATORS = {"ilike", "like", "=ilike", "=like"}


def _split_search_tokens(name):
    return [token for token in re.split(r"[\s,;/|()\-]+", (name or "").strip()) if token]


def _build_product_candidate_domain(name, operator, lot_product):
    domains = [
        [("display_name", operator, name)],
        [("name", operator, name)],
        [("barcode", operator, name)],
        [("default_code", operator, name)],
        [("product_template_variant_value_ids.name", operator, name)],
    ]

    tokens = _split_search_tokens(name)
    if len(tokens) > 1:
        domains.append(expression.AND([[("display_name", operator, token)] for token in tokens]))
        domains.append(
            expression.AND([[("product_template_variant_value_ids.name", operator, token)] for token in tokens])
        )

    if lot_product:
        domains.append([("id", "=", lot_product.id)])

    return expression.OR(domains)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model
    def _name_search(self, name, domain=None, operator="ilike", limit=None, order=None):
        domain = list(domain or [])
        result_ids = super()._name_search(
            name=name,
            domain=domain,
            operator=operator,
            limit=limit,
            order=order,
        )
        if not name or operator not in SUPPORTED_TEXT_OPERATORS:
            return result_ids

        lot_product = self.env["stock.lot"].search([("name", "=", name)], limit=1).product_id
        product_domain = _build_product_candidate_domain(name, operator, lot_product)
        product_ids = self.env["product.product"]._search(
            product_domain,
            limit=limit or 100,
        )
        if not product_ids:
            return result_ids

        template_domain = expression.AND([domain, [("product_variant_ids", "in", product_ids)]])
        variant_template_ids = self._search(template_domain, limit=limit or 100, order=order)

        # Prioritize variant-driven matches, then keep original behavior.
        merged = []
        seen = set()
        for template_id in variant_template_ids + result_ids:
            if template_id not in seen:
                seen.add(template_id)
                merged.append(template_id)

        return merged[:limit] if limit else merged


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        args = list(args or [])
        result = super().name_search(name=name, args=args, operator=operator, limit=limit)
        if not name or operator not in SUPPORTED_TEXT_OPERATORS:
            return result

        lot_product = self.env["stock.lot"].search([("name", "=", name)], limit=1).product_id
        product_domain = _build_product_candidate_domain(name, operator, lot_product)
        fallback_domain = expression.AND([args, product_domain])
        products = self.search(fallback_domain, limit=limit or 100)

        variant_results = [(product.id, product.display_name) for product in products]
        merged = []
        seen = set()

        # Prioritize variant/display-name matches, then keep base name_search output.
        for product_id, product_name in variant_results + result:
            if product_id not in seen:
                seen.add(product_id)
                merged.append((product_id, product_name))

        return merged[:limit] if limit else merged


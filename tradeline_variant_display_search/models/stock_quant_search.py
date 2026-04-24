# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models
from odoo.osv import expression

from .product_search import SUPPORTED_TEXT_OPERATORS

TOKEN_BOUNDARY_RE = r"(?<![a-z0-9])%s(?![a-z0-9])"


def _split_search_tokens(value):
    tokens = [token.lower() for token in re.split(r"[\s,;/|()\-]+", (value or "").strip()) if token]
    return [token for token in tokens if len(token) >= 2 or token.isdigit()]


def _token_present(text, token):
    haystack = (text or "").lower()
    if not haystack:
        return False
    if re.fullmatch(r"[a-z0-9]+", token):
        pattern = TOKEN_BOUNDARY_RE % re.escape(token)
        return bool(re.search(pattern, haystack))
    return token in haystack


def _product_broad_text(product):
    attrs_text = " ".join(product.product_template_variant_value_ids.mapped("name"))
    return " ".join(
        filter(
            None,
            [
                product.display_name,
                product.name,
                product.product_tmpl_id.name,
                attrs_text,
                product.barcode,
                product.default_code,
            ],
        )
    ).lower()


def _token_any_field_domain(token, operator):
    return expression.OR(
        [
            [("display_name", operator, token)],
            [("name", operator, token)],
            [("product_tmpl_id.name", operator, token)],
            [("product_template_variant_value_ids.name", operator, token)],
            [("barcode", operator, token)],
            [("default_code", operator, token)],
        ]
    )


def _search_product_ids_for_broad(env, value, operator, limit=5000):
    value = (value or "").strip()
    if not value:
        return []

    effective_operator = operator if operator in {"ilike", "like", "=ilike", "=like"} else "ilike"
    tokens = _split_search_tokens(value)
    if len(tokens) <= 1:
        product_matches = env["product.product"].name_search(
            name=value,
            operator=effective_operator,
            limit=limit,
        )
        return [product_id for product_id, _name in product_matches]

    token_domain = expression.AND([_token_any_field_domain(token, effective_operator) for token in tokens])
    products = env["product.product"].search(token_domain, limit=limit)
    return [product.id for product in products if all(_token_present(_product_broad_text(product), token) for token in tokens)]


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

        tokens = _split_search_tokens(value)
        if len(tokens) <= 1:
            effective_operator = operator if operator in SUPPORTED_TEXT_OPERATORS else "ilike"
            products = self.env["product.product"].search(
                [("display_name", effective_operator, value)],
                limit=5000,
            )
        else:
            token_domain = expression.AND([[("display_name", "ilike", token)] for token in tokens])
            products = self.env["product.product"].search(token_domain, limit=5000).filtered(
                lambda product: all(_token_present((product.display_name or "").lower(), token) for token in tokens)
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

            product_ids = _search_product_ids_for_broad(self.env, value, operator, limit=5000)
            if not product_ids:
                return ("id", "=", 0)
            return ("product_id", "in", product_ids)

        def _rewrite(node):
            if (
                isinstance(node, (list, tuple))
                and len(node) >= 3
                and isinstance(node[0], str)
                and node[0] not in {"|", "&", "!"}
            ):
                return _rewrite_leaf(node)
            if isinstance(node, list):
                return [_rewrite(item) for item in node]
            return node

        return _rewrite(domain)

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None, *args, **kwargs):
        """Ensure product_id text filters are normalized in all ORM code paths.

        Odoo list/pager loading commonly calls _search (via search_fetch/web_search_read)
        directly, so rewriting only in search() misses some UI requests.
        """
        domain = self._rewrite_product_id_text_domain(domain)
        return super()._search(
            domain,
            offset=offset,
            limit=limit,
            order=order,
            *args,
            **kwargs,
        )

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

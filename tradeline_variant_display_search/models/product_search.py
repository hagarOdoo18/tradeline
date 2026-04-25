# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models
from odoo.osv import expression


SUPPORTED_TEXT_OPERATORS = {"ilike", "like", "=ilike", "=like"}
TOKEN_BOUNDARY_RE = r"(?<![a-z0-9])%s(?![a-z0-9])"


def _split_search_tokens(name):
    tokens = [token.lower() for token in re.split(r"[\s,;/|()\-]+", (name or "").strip()) if token]
    # Keep single-digit tokens (e.g. "1 Pack", "Flip 5") so numeric variants
    # are not treated as equivalent.
    return [token for token in tokens if len(token) >= 2 or token.isdigit()]


def _token_text_fields_domain(token, operator):
    return expression.OR(
        [
            [("display_name", operator, token)],
            [("name", operator, token)],
            [("product_tmpl_id.name", operator, token)],
            [("product_template_variant_value_ids.name", operator, token)],
        ]
    )


def _token_any_field_domain(token, operator):
    # Numeric tokens like "1" or "5" should match product text fields only.
    # If they also match item codes/barcodes, unrelated products can leak in.
    if token.isdigit():
        return _token_text_fields_domain(token, operator)
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


def _build_product_candidate_domain(name, operator, lot_product):
    domains = [
        [("display_name", operator, name)],
        [("name", operator, name)],
        [("product_tmpl_id.name", operator, name)],
        [("barcode", operator, name)],
        [("default_code", operator, name)],
        [("product_template_variant_value_ids.name", operator, name)],
    ]

    tokens = _split_search_tokens(name)
    if len(tokens) > 1:
        domains.append(expression.AND([[("display_name", operator, token)] for token in tokens]))
        domains.append(expression.AND([[("name", operator, token)] for token in tokens]))
        domains.append(expression.AND([[("product_tmpl_id.name", operator, token)] for token in tokens]))
        domains.append(
            expression.AND([[("product_template_variant_value_ids.name", operator, token)] for token in tokens])
        )
        # Critical: allow tokens to be distributed across template name + variant values.
        per_token_any_field = [_token_any_field_domain(token, operator) for token in tokens]
        domains.append(expression.AND(per_token_any_field))

    if lot_product:
        domains.append([("id", "=", lot_product.id)])

    return expression.OR(domains)


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


def _normalize_phrase_text(value):
    normalized = re.sub(r"[^\w]+", " ", (value or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _phrase_present(text, query):
    normalized_query = _normalize_phrase_text(query)
    if not normalized_query:
        return False
    normalized_text = _normalize_phrase_text(text)
    return normalized_query in normalized_text


def _all_tokens_match_ordered_relaxed(text, tokens):
    haystack = (text or "").lower()
    if not haystack:
        return False
    start_at = 0
    for token in tokens:
        idx = haystack.find(token, start_at)
        if idx == -1:
            return False
        start_at = idx + len(token)
    return True


def _all_tokens_match_relaxed(text, tokens):
    haystack = (text or "").lower()
    return all(token in haystack for token in tokens)


def resolve_broad_product_ids(env, value, operator="ilike", limit=5000):
    """Resolve product ids for broad text queries using Product Variants ranking.

    This is intentionally shared by report models (stock.quant/account.invoice.report)
    so broad search behavior stays aligned with product.product name_search.
    """
    value = (value or "").strip()
    if not value:
        return []

    effective_operator = operator if operator in SUPPORTED_TEXT_OPERATORS else "ilike"
    product_matches = env["product.product"].name_search(
        name=value,
        args=[],
        operator=effective_operator,
        limit=limit,
    )
    product_ids = _ordered_unique_ids([product_id for product_id, _name in product_matches])

    tokens = _split_search_tokens(value)
    if len(tokens) <= 1:
        return product_ids

    products_by_id = {product.id: product for product in env["product.product"].browse(product_ids).exists()}
    phrase_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _phrase_present(_product_broad_text(products_by_id[product_id]), value)
    ]
    if phrase_ids:
        return phrase_ids

    strict_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and all(_token_present(_product_broad_text(products_by_id[product_id]), token) for token in tokens)
    ]
    if strict_ids:
        return strict_ids

    ordered_relaxed_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _all_tokens_match_ordered_relaxed(_product_broad_text(products_by_id[product_id]), tokens)
    ]
    if ordered_relaxed_ids:
        return ordered_relaxed_ids

    relaxed_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _all_tokens_match_relaxed(_product_broad_text(products_by_id[product_id]), tokens)
    ]
    return relaxed_ids or product_ids


def _score_product(product, full_term, tokens):
    display_name = (product.display_name or "").lower()
    name = (product.name or "").lower()
    template_name = (product.product_tmpl_id.name or "").lower()
    barcode = (product.barcode or "").lower()
    default_code = (product.default_code or "").lower()
    attrs_text = " ".join(product.product_template_variant_value_ids.mapped("name")).lower()

    score = 0
    if display_name == full_term:
        score += 1000
    if name == full_term:
        score += 900
    if barcode == full_term or default_code == full_term:
        score += 900

    if full_term and full_term in display_name:
        score += 700
    if full_term and full_term in name:
        score += 600
    if full_term and full_term in template_name:
        score += 580
    if full_term and full_term in attrs_text:
        score += 550

    display_hits = sum(1 for token in tokens if _token_present(display_name, token))
    name_hits = sum(1 for token in tokens if _token_present(name, token))
    template_hits = sum(1 for token in tokens if _token_present(template_name, token))
    attr_hits = sum(1 for token in tokens if _token_present(attrs_text, token))

    score += display_hits * 50
    score += name_hits * 35
    score += template_hits * 35
    score += attr_hits * 20

    if tokens and display_hits == len(tokens):
        score += 300
    if tokens and name_hits == len(tokens):
        score += 220
    if tokens and template_hits == len(tokens):
        score += 220
    if tokens and attr_hits == len(tokens):
        score += 160
    if tokens and (template_hits + attr_hits) >= len(tokens):
        score += 260

    return score


def _rank_products(products, search_term, base_order):
    tokens = _split_search_tokens(search_term)
    full_term = (search_term or "").strip().lower()

    ranked = []
    for product in products:
        score = _score_product(product, full_term, tokens)
        base_index = base_order.get(product.id, 10**6)
        ranked.append((score, base_index, product))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2].id))
    return [item[2] for item in ranked]


def _ordered_unique_ids(ids):
    ordered = []
    seen = set()
    for value in ids:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _strict_ranked_products(ranked_products, search_term):
    full_term = (search_term or "").strip().lower()
    if not full_term:
        return []

    exact = []
    for product in ranked_products:
        if full_term in {
            (product.display_name or "").strip().lower(),
            (product.name or "").strip().lower(),
            (product.barcode or "").strip().lower(),
            (product.default_code or "").strip().lower(),
        }:
            exact.append(product)
    if exact:
        return exact

    numeric_tokens = [token for token in _split_search_tokens(search_term) if token.isdigit()]
    if not numeric_tokens:
        return []

    strict = []
    for product in ranked_products:
        attrs_text = " ".join(product.product_template_variant_value_ids.mapped("name"))
        product_text = " ".join(
            filter(
                None,
                [
                    product.display_name,
                    product.name,
                    product.product_tmpl_id.name,
                    attrs_text,
                ],
            )
        ).lower()
        if all(_token_present(product_text, token) for token in numeric_tokens):
            strict.append(product)
    return strict


class ProductTemplate(models.Model):
    _inherit = "product.template"

    product_template_lookup_id = fields.Many2one(
        "product.template",
        string="Product (Broad)",
        compute="_compute_product_template_lookup_id",
        search="_search_product_template_lookup_id",
        store=False,
    )

    def _compute_product_template_lookup_id(self):
        for product_template in self:
            product_template.product_template_lookup_id = product_template

    def _search_product_template_lookup_id(self, operator, value):
        if operator in {"=", "=="} and isinstance(value, int):
            return [("id", "=", value)]
        if operator in {"!=", "<>"} and isinstance(value, int):
            return [("id", "!=", value)]
        if operator == "in" and isinstance(value, (list, tuple, set)):
            template_ids = [template_id for template_id in value if isinstance(template_id, int)]
            if not template_ids:
                return [("id", "=", 0)]
            return [("id", "in", template_ids)]

        if not isinstance(value, str):
            return []

        search_term = value.strip()
        if not search_term:
            return []

        effective_operator = operator if operator in SUPPORTED_TEXT_OPERATORS else "ilike"
        matches = super(ProductTemplate, self)._name_search(
            name=search_term,
            domain=[],
            operator=effective_operator,
            limit=5000,
            order=None,
        )
        template_ids = [template_id for template_id, _name in matches]
        if not template_ids:
            return [("id", "=", 0)]
        return [("id", "in", template_ids)]

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
        product_limit = max((limit or 100) * 10, 200)
        products = self.env["product.product"].search(
            expression.AND([product_domain]),
            limit=product_limit,
        )
        if not products:
            return result_ids

        product_base_order = {product.id: idx for idx, product in enumerate(products)}
        tokens = _split_search_tokens(name)
        ranked_products = _rank_products(products, name, product_base_order)
        strict_products = _strict_ranked_products(ranked_products, name)
        effective_products = strict_products or ranked_products
        template_ids_by_rank = _ordered_unique_ids([product.product_tmpl_id.id for product in effective_products])
        prefer_ranked_only = bool(strict_products) or len(tokens) > 1

        # Keep template record rules/domains intact for caller context.
        candidate_ids = template_ids_by_rank if prefer_ranked_only else template_ids_by_rank + result_ids
        visible_template_ids = self._search(
            expression.AND([domain, [("id", "in", candidate_ids)]]),
            order=order,
        )
        visible_set = set(visible_template_ids)

        merged = []
        seen = set()
        ordered_candidates = candidate_ids
        for template_id in ordered_candidates:
            if template_id in visible_set and template_id not in seen:
                seen.add(template_id)
                merged.append(template_id)

        # Append remaining visible templates if any.
        if not prefer_ranked_only:
            for template_id in visible_template_ids:
                if template_id not in seen:
                    seen.add(template_id)
                    merged.append(template_id)

        return merged[:limit] if limit else merged


class ProductProduct(models.Model):
    _inherit = "product.product"

    product_lookup_id = fields.Many2one(
        "product.product",
        string="Product",
        compute="_compute_product_lookup_id",
        search="_search_product_lookup_id",
        store=False,
    )

    def _compute_product_lookup_id(self):
        for product in self:
            product.product_lookup_id = product

    def _search_product_lookup_id(self, operator, value):
        if operator in {"=", "=="} and isinstance(value, int):
            return [("id", "=", value)]
        if operator in {"!=", "<>"} and isinstance(value, int):
            return [("id", "!=", value)]
        if operator == "in" and isinstance(value, (list, tuple, set)):
            product_ids = [product_id for product_id in value if isinstance(product_id, int)]
            if not product_ids:
                return [("id", "=", 0)]
            return [("id", "in", product_ids)]

        if not isinstance(value, str):
            return []

        search_term = value.strip()
        if not search_term:
            return []

        effective_operator = operator if operator in SUPPORTED_TEXT_OPERATORS else "ilike"
        matches = self.name_search(name=search_term, args=[], operator=effective_operator, limit=5000)
        product_ids = [product_id for product_id, _name in matches]
        if not product_ids:
            return [("id", "=", 0)]
        return [("id", "in", product_ids)]

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        args = list(args or [])
        result = super().name_search(name=name, args=args, operator=operator, limit=limit)
        if not name or operator not in SUPPORTED_TEXT_OPERATORS:
            return result

        lot_product = self.env["stock.lot"].search([("name", "=", name)], limit=1).product_id
        product_domain = _build_product_candidate_domain(name, operator, lot_product)
        fallback_domain = expression.AND([args, product_domain])
        product_limit = max((limit or 100) * 10, 200)
        products = self.search(fallback_domain, limit=product_limit)
        if not products:
            return result

        base_order = {product_id: idx for idx, (product_id, _) in enumerate(result)}
        tokens = _split_search_tokens(name)
        ranked_products = _rank_products(products, name, base_order)
        strict_products = _strict_ranked_products(ranked_products, name)
        effective_products = strict_products or ranked_products
        variant_results = [(product.id, product.display_name) for product in effective_products]

        # For exact or multi-token searches, avoid re-introducing broad base name_search hits.
        if strict_products or len(tokens) > 1:
            return variant_results[:limit] if limit else variant_results

        merged = []
        seen = set()

        # Prioritize strong variant/display-name matches, then keep base name_search output.
        for product_id, product_name in variant_results + result:
            if product_id not in seen:
                seen.add(product_id)
                merged.append((product_id, product_name))

        return merged[:limit] if limit else merged


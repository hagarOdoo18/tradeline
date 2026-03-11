# -*- coding: utf-8 -*-

SUPPORTED_TEXT_OPERATORS = {"ilike", "like", "=ilike", "=like"}


def search_product_ids_by_text(env, value, operator="ilike", limit=5000):
    value = (value or "").strip()
    if not value:
        return []

    product_matches = env["product.product"].name_search(
        name=value,
        operator=operator,
        limit=limit,
    )
    return [product_id for product_id, _name in product_matches]


def rewrite_product_id_text_domain(env, domain):
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

        product_ids = search_product_ids_by_text(env, value, operator=operator, limit=5000)
        if not product_ids:
            return ("id", "=", 0)
        return ("product_id", "in", product_ids)

    def _rewrite(node):
        rewritten_leaf = _rewrite_leaf(node)
        if rewritten_leaf is not node:
            return rewritten_leaf

        if isinstance(node, list):
            return [_rewrite(item) for item in node]
        return node

    return _rewrite(domain)

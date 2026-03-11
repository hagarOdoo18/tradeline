# -*- coding: utf-8 -*-

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

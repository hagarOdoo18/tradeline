# -*- coding: utf-8 -*-

APPLE_BUSINESS_CATEGORY_NAMES = {"mac", "ipad", "iphone"}


def is_apple_business_category(category):
    seen = set()
    current = category
    while current and current.id not in seen:
        if current.name.strip().lower() in APPLE_BUSINESS_CATEGORY_NAMES:
            return True
        seen.add(current.id)
        current = current.parent_id
    return False


def is_apple_business_device(product):
    return bool(
        product.vendor_id
        and product.vendor_id.name.strip().lower() == "abm"
        and is_apple_business_category(product.categ_id)
    )

# -*- coding: utf-8 -*-
{
    "name": "Product Business Customer Only",
    "version": "18.0.1.0.0",
    "category": "Sales/Sales",
    "summary": "Restrict selected products to business customers in Sales and POS",
    "author": "Tradeline",
    "depends": [
        "sale_management",
        "point_of_sale",
        "partner_customization",
    ],
    "data": [
        "views/product_template_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "product_business_customer_only/static/src/js/business_customer_only.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}

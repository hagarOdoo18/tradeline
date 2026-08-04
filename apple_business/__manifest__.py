# -*- coding: utf-8 -*-
{
    "name": "Apple Business",
    "version": "18.0.2.2.0",
    "category": "Sales",
    "summary": "Manage Apple Business subscriptions and ABM-only sales orders",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "sale_management",
        "sale_stock",
        "account",
        "branch",
        "inventory_customization",
        "partner_customization",
    ],
    "data": [
        "security/apple_business_security.xml",
        "security/ir.model.access.csv",
        "views/apple_business_views.xml",
        "views/sale_order_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "apple_business/static/src/js/apple_business_boolean_field.js",
        ],
    },
    "installable": True,
    "application": True,
}

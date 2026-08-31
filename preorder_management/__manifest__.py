# -*- coding: utf-8 -*-
{
    "name": "Tradeline Pre-order Management",
    "version": "18.0.5.0.0",
    "category": "Sales",
    "summary": "Branch allocations and payment reuse for product pre-orders",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "sale_management",
        "sale_stock",
        "point_of_sale",
        "account",
        "base_tradeline",
        "branch",
        "accounting_customization",
        "sale_payment_return",
    ],
    "data": [
        "security/preorder_security.xml",
        "security/ir.model.access.csv",
        "data/preorder_sequence.xml",
        "report/preorder_confirmation_report.xml",
        "views/preorder_views.xml",
        "views/sale_order_views.xml",
    ],
    "installable": True,
    "application": True,
}

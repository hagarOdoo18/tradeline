{
    "name": "Tradeline Sale Order Line Report 18",
    "summary": "Odoo 18 sale order line report adapted from the legacy Tradeline Odoo 12 view",
    "version": "18.0.1.0.0",
    "category": "Sales/Sales",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "sale",
        "sales_team",
        "product",
        "account",
    ],
    "data": [
        "security/security.xml",
        "views/sale_order_line_report_views.xml",
    ],
    "installable": True,
    "application": True,
}

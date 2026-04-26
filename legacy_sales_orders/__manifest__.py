{
    "name": "Legacy Sales Orders",
    "summary": "Odoo12 quotations and sale order lines parity on legacy data",
    "version": "18.0.1.0.0",
    "category": "Sales",
    "author": "Tradeline",
    "website": "http://www.tradelinestores.com",
    "license": "LGPL-3",
    "depends": ["sale", "legacy_invoice_archive"],
    "data": [
        "security/ir.model.access.csv",
        "views/legacy_sales_order_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}


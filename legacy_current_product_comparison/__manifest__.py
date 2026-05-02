{
    "name": "Legacy Current Product Comparison",
    "summary": "Monthly legacy (Odoo12) vs current (Odoo18) product comparison",
    "version": "18.0.1.0.4",
    "category": "Accounting",
    "author": "Tradeline",
    "website": "http://www.tradelinestores.com",
    "license": "LGPL-3",
    "depends": ["legacy_invoice_archive", "account", "stock"],
    "data": [
        "security/ir.model.access.csv",
        "views/legacy_current_product_compare_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}

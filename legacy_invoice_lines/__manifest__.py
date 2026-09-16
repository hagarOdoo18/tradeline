{
    "name": "Legacy Invoice Lines",
    "summary": "Odoo12 Invoice Lines parity view on legacy archive data",
    "version": "18.0.1.0.0",
    "category": "Accounting",
    "author": "Tradeline",
    "website": "http://www.tradelinestores.com",
    "license": "LGPL-3",
    "depends": ["legacy_invoice_archive"],
    "data": [
        "views/legacy_invoice_line_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}


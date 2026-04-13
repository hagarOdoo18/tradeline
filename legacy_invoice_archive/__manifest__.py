{
    "name": "Legacy Invoice Archive",
    "summary": "Read-only Odoo 12 invoice archive for Odoo 18",
    "version": "18.0.1.0.14",
    "category": "Accounting",
    "author": "Tradeline",
    "website": "http://www.tradelinestores.com",
    "license": "LGPL-3",
    "depends": ["account", "stock"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/legacy_invoice_report.xml",
        "views/legacy_report_pack_report.xml",
        "views/legacy_invoice_views.xml",
        "views/legacy_report_pack_views.xml",
        "views/legacy_analysis_views.xml",
        "views/legacy_report_pack_generate_wizard_views.xml",
        "views/res_partner_views.xml"
    ],
    "installable": True,
    "application": False
}

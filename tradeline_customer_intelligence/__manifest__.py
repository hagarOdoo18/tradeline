{
    "name": "Tradeline Customer Intelligence",
    "summary": "Product, customer, bundle and launch intelligence for Tradeline executives",
    "version": "18.0.1.3.5",
    "category": "Sales/CRM",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "web",
        "account",
        "sale_management",
        "legacy_invoice_archive",
        "legacy_invoice_lines",
        "legacy_current_product_comparison",
    ],
    "data": [
        "security/security.xml",
        "views/intelligence_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tradeline_customer_intelligence/static/src/js/intelligence_action.js",
            "tradeline_customer_intelligence/static/src/xml/intelligence_templates.xml",
            "tradeline_customer_intelligence/static/src/scss/intelligence.scss",
        ],
    },
    "application": True,
    "installable": True,
    "auto_install": False,
}

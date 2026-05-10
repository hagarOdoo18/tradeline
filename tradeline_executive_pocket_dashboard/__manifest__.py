# -*- coding: utf-8 -*-
{
    "name": "Tradeline Executive Pocket Dashboard",
    "summary": "Standalone executive dashboard with granular drilldowns and live FX watch",
    "version": "18.0.1.0.0",
    "category": "Reporting",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "web",
        "account",
        "sale_management",
        "stock",
        "crm",
    ],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/executive_dashboard_views.xml",
        "views/executive_fx_rate_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tradeline_executive_pocket_dashboard/static/src/js/executive_dashboard_action.js",
            "tradeline_executive_pocket_dashboard/static/src/xml/executive_dashboard_templates.xml",
            "tradeline_executive_pocket_dashboard/static/src/scss/executive_dashboard.scss",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}


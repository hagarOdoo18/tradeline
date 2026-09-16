# -*- coding: utf-8 -*-
{
    "name": "Tradeline AI BI Copilot",
    "summary": "Read-only BI copilot for internal Odoo users",
    "version": "18.0.1.0.0",
    "category": "Productivity",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/default_data.xml",
        "views/menu_views.xml",
        "views/settings_views.xml",
        "views/conversation_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tradeline_ai_copilot/static/src/js/copilot_widget.js",
            "tradeline_ai_copilot/static/src/xml/copilot_widget.xml",
            "tradeline_ai_copilot/static/src/scss/copilot_widget.scss",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
    "post_init_hook": "post_init_hook",
}

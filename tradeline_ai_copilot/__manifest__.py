# -*- coding: utf-8 -*-
{
    "name": "Tradeline AI BI Copilot",
    "summary": "Strategic reporting chat with legacy cohorts, basket analysis and cited web research",
    "version": "18.0.3.3.0",
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
            "tradeline_ai_copilot/static/src/js/copilot_chart.js",
            "tradeline_ai_copilot/static/src/js/copilot_stream.js",
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

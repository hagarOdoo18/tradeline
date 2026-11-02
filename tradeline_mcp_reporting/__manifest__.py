{
    "name": "Tradeline Read-only MCP Reporting",
    "version": "18.0.1.3.3",
    "summary": "Read-only Odoo reporting for Claude Desktop, web and mobile",
    "category": "Productivity",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": ["tradeline_ai_copilot"],
    "data": ["security/security.xml", "security/ir.model.access.csv", "views/policy_views.xml"],
    "external_dependencies": {"python": ["phonenumbers"]},
    "installable": True,
    "application": False,
    "auto_install": False,
}

{
    "name": "Service Scrap Control",
    "version": "18.0.1.0.11",
    "category": "Inventory",
    "summary": "Replicate Odoo12 service scrap request/approve/vendor workflows in Odoo18",
    "depends": ["stock", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/wizard_views.xml",
        "views/stock_picking_views.xml",
        "views/stock_scrap_views.xml",
        "views/stock_picking_type_views.xml"
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "license": "LGPL-3"
}

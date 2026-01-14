{
    "name": "Stock Transfer Import Excel (Odoo 18)",
    "version": "18.0.1.1.0",
    "category": "Inventory",
    "summary": "Import Excel to stock transfer with preview and update existing moves",
    "depends": ["stock"],
    "data": [
        "security/ir.model.access.csv",
        "views/import_transfer_excel_view.xml",
        "views/stock_picking_view.xml"
    ],
    "installable": True,
    "license": "LGPL-3",
}
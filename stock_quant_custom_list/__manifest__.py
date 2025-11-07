{
    "name": "Stock Quant Custom List",
    "version": "1.0.0",
    "summary": "Custom list view for stock.quant with related product fields and restricted menu group",
    "author": "Ezzat",
    "category": "Inventory",
    "depends": ["stock", "product",'inventory_customization'],
    "data": [
        "security/stock_quant_custom_security.xml",
        "views/stock_quant_view.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False
}

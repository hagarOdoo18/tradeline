{
    "name": "Product Attribute Import",
    "version": "18.0.1.0.0",
    "category": "Product",
    "summary": "Import product attributes and values from Excel",
    "author": "Ezzat",
    "depends": ["stock"],
    "data": [
        'security/ir.model.access.csv',
        "views/product_attribute_import_wizard_view.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}

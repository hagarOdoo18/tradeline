# -*- coding: utf-8 -*-
{
    "name": "Update Product Codes by Barcode",
    "version": "18.0.1.0.0",
    "summary": "Update e_code and gs1_code on product.product by barcode from Excel",
    "description": "Wizard to import an Excel file and update product.product e_code and gs1_code fields by matching barcode.",
    "author": "ezzat",
    "depends": ["stock"],
    "data": [
        "security/ir.model.access.csv",
        "views/update_product_codes_view.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}

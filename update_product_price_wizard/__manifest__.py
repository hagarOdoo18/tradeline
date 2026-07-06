{
    'name': 'Update Product Price Wizard',
    'version': '18.0.1.0.0',
    'summary': 'Update product sales prices by uploading an Excel sheet',
    'description': """
        Wizard to bulk-update product sales prices (list_price) from a
        spreadsheet with two columns:
          • Item Code  – product internal reference (default_code) or barcode
          • New Price  – new sales price value
    """,
    'category': 'Inventory/Products',
    'author': 'Tradeline',
    'depends': ['product'],
    'data': [
        'security/ir.model.access.csv',
        'views/update_product_price_wizard_view.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

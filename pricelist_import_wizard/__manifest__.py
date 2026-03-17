{
    'name': 'Pricelist Import Wizard',
    'version': '18.0.1.0.0',
    'summary': 'Import pricelist items from Excel/CSV sheet',
    'description': """
        Wizard to import pricelist items from a spreadsheet.
        Supports item_code (product internal reference) and fixed_price columns.
        Automatically applies:
          - applied_on = '1_product' (Product level)
          - compute_price = 'fixed'
          - Currency and Company inherited from the pricelist
    """,
    'category': 'Sales/Sales',
    'author': 'Tradeline',
    'depends': ['product', 'sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/pricelist_import_wizard_view.xml',
        'views/product_pricelist_view.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

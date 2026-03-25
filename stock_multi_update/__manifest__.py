{
    'name': 'Stock Multi Product Update',
    'version': '18.0.2.0.0',
    'summary': 'Add or subtract quantity with serial/lot for multiple products — saved records',
    'category': 'Inventory/Inventory',
    'author': 'Tradeline',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/stock_multi_update_views.xml',
        'views/stock_multi_update_menu.xml',
        'wizard/stock_multi_update_import_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

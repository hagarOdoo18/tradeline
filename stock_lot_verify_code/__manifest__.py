{
    'name': 'Stock Lot Verify Code',
    'summary': 'Adds a Verify Code on lots/serials and imports it from an Excel file (serial, code).',
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'category': 'Inventory',
    'depends': ['stock'],
    'external_dependencies': {'python': ['openpyxl']},
    'data': [
        'security/ir.model.access.csv',
        'views/stock_lot_views.xml',
        'views/stock_move_line_views.xml',
        'wizard/verify_code_import_wizard_views.xml',
    ],
    'installable': True,
}

{
    'name': 'Import Stock Quant From Excel',
    'version': '18.0.1.0.0',
    'category': 'Inventory',
    'summary': 'Import stock using Excel with preview and error handling',
    'depends': ['stock'],
    'data': [
        'security/import_stock_quant_groups.xml',
        'security/ir.model.access.csv',
        'views/import_stock_quant_wizard_view.xml',
    ],
    'installable': True,
}
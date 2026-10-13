{
    'name': 'Import Stock Quant From Excel',
    'license': 'LGPL-3',
    'version': '18.0.1.0.4',
    'category': 'Inventory',
    'summary': 'Import stock using Excel with preview and error handling',
    'depends': ['stock_account'],
    'data': [
        'security/import_stock_quant_groups.xml',
        'security/ir.model.access.csv',
        'security/import_stock_quant_rules.xml',
        'data/import_stock_quant_cron.xml',
        'views/import_stock_quant_wizard_view.xml',
    ],
    'installable': True,
}

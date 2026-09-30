{
    'name': 'Import Stock Quant From Excel',
    'license': 'LGPL-3',
    'version': '18.0.1.0.2',
    'category': 'Inventory',
    'summary': 'Import stock using Excel with preview and error handling',
    'depends': ['stock', 'stock_valuation_layer_extend'],
    'data': [
        'security/import_stock_quant_groups.xml',
        'security/ir.model.access.csv',
        'views/import_stock_quant_wizard_view.xml',
    ],
    'installable': True,
}

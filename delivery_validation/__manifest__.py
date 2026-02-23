{
    'name': 'Stock Picking Auto Validation',
    'version': '18.0.1.0.0',
    'summary': 'Auto-validates Deliveries and Receipts (stock.picking) 5 minutes after creation + duplicate serial fix',
    'author': 'tradeline',
    'category': 'Inventory',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/stock_picking_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

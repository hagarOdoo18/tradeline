{
    'name': 'Download Picking Barcodes',
    'version': '1.0',
    'summary': 'Download all product barcodes from picking lines',
    'author': 'Ezzat',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/download_picking_barcodes_wizard_view.xml',
        'views/stock_picking_view.xml',
    ],
    'installable': True,
    'application': False,
}

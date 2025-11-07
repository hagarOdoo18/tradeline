{
    'name': 'picking_upload_serial',
    'version': '1.0',
    'summary': 'Upload Excel to stock picking (serials & quantities)',
    'description': 'Upload .xlsx file (Code, Serial, Quantity) to update stock picking lines, create lots and optionally confirm the picking.',
    'author': 'ChatGPT (generated)',
    'license': 'LGPL-3',
    'depends': ['stock', 'product'],
    'data': [
        'security/ir.model.access.csv',
        'views/upload_delivery_wizard_view.xml',
    ],
    'installable': True,
    'application': False,
}

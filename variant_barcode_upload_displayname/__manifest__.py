# -*- coding: utf-8 -*-
{
    'name': 'Variant Barcode Upload (by Display Name)',
    'version': '1.0',
    'author': 'ChatGPT',
    'category': 'Inventory',
    'summary': 'Upload product variant barcodes using display name from Excel',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/upload_barcode_wizard_view.xml'
             ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

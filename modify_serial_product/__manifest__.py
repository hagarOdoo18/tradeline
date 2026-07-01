# -*- coding: utf-8 -*-
{
    'name': 'Modify Serial Product',
    'summary': 'Wizard to reassign serial/lot numbers from one product to another via Excel upload',
    'author': 'Tradeline',
    'website': 'http://www.tradelinestores.com',
    'category': 'Inventory',
    'version': '18.0.1.0.0',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/modify_serial_wizard_views.xml',
    ],
    'application': False,
}

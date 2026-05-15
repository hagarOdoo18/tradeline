# -*- coding: utf-8 -*-
{
    'name': 'Tradeline Accounting Excel Report',
    'author': 'Tradeline',
    'website': 'http://www.tradeline.com',
    'category': 'Accounting',
    'version': '18.0.1.0.0',
    'depends': ['base', 'account', 'sale_management'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/account_invoice_wizard.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}

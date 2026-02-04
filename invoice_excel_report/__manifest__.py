
# -*- coding: utf-8 -*-
{
    'name': 'Invoice Excel Report Wizard',
    'version': '1.0',
    'summary': 'Export Account Invoices to Excel',
    'description': 'Wizard to export account invoices filtered by customer, store, date, journal, etc. to Excel.',
    'category': 'Accounting',
    'author': 'tradeline',
    'website': 'https://tradeline.com',
    'depends': ['base', 'account', 'branch'],
    'data': [
        'security/ir.model.access.csv',
        'views/invoice_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

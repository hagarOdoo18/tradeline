
# -*- coding: utf-8 -*-
{
    'name': 'Invoice Excel Report Wizard',
    'version': '18.0.1.3.0',
    'summary': 'View and export account invoice payment reports',
    'description': 'View or export account invoices filtered by customer, store, date, journal, etc.',
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

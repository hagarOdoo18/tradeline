# -*- coding: utf-8 -*-
{
    'name': 'Branch Account Excel Report',
    'version': '18.0.1.0.0',
    'category': 'Accounting',
    'summary': 'Multi-company Branch Excel Report with Summary and Payment Report',
    'depends': ['account', 'sale', 'branch'],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_report_views.xml',
        'wizard/account_branch_report_wizard_view.xml',
    ],
    'installable': True,
}

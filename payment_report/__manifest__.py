# -*- coding: utf-8 -*-
{
    'name': 'Payment Report',
    'version': '18.0.1.0.0',
    'category': 'Accounting/Reporting',
    'summary': 'Per-branch payment report (invoices, credit notes, order payments)',
    'description': """
Payment Report
==============
SQL-view-backed report combining:

* Posted out_invoices reconciled to an account.payment
* Posted out_invoices via POS (no account.payment)
* Posted out_refunds reconciled to an account.payment (negative)
* Posted out_refunds via POS
* account.payment with sale_order_id, state='paid' (signed by payment_type)

Branch and POS columns are detected at runtime — the report works even
if those modules aren't installed.
""",
    'author': 'Tradeline',
    'website': 'https://Tradeline.com',
    'depends': [
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_report_views.xml',
        'views/payment_report_menu.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

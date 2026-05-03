# -*- coding: utf-8 -*-
{
    'name': 'Payment Report',
    'version': '18.0.1.0.0',
    'category': 'Accounting/Reporting',
    'summary': 'Per-branch payment report (invoices, credit notes, order payments)',
    'description': """
Payment Report
==============
Generate a per-branch payment report combining:

* Posted out_invoices and their reconciled (or POS) payments
* Posted out_refund credit notes (negative amounts)
* account.payment records linked to a sale order

Output is rendered as list, pivot and graph views, plus a per-branch summary
(record count, total invoice, total payment, difference) and an optional
QWeb PDF.
""",
    'author': 'Tradeline',
    'website': 'https://Tradeline.com',
    # NOTE: requires a multi-branch module that provides `res.branch` and adds
    # `branch_id` to account.move and account.payment. Adjust the dep below to
    # match the branch module installed in your database.
    'depends': [
        'account',
        'sale_management',
        'point_of_sale',
        'branch',
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

{
    'name': 'Custom Invoice Header',
    'license': 'LGPL-3',
    'version': '18.0.1.1.0',
    'author': 'tradeline',
    'category': 'Accounting',
    'summary': 'Customize invoice report header',
    'depends': [
        'account',
        'stock_account',
        'l10n_eg_edi_eta',
        'accounting_customization',
    ],
    'data': [
        'views/report_invoice_header.xml',
        'views/transfer_report.xml',
        'views/sale_order_print_payment.xml',
    ],
    'installable': True,
    'application': False,
}

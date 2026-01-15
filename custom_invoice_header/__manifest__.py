{
    'name': 'Custom Invoice Header',
    'version': '1.0',
    'author': 'tradeline',
    'category': 'Accounting',
    'summary': 'Customize invoice report header',
    'depends': ['account','stock_account'],
    'data': [
        'views/report_invoice_header.xml',
        'views/transfer_report.xml',
    ],
    'installable': True,
    'application': False,
}

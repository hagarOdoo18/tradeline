{
    'name': 'Sales Invoice Lines View',
    'version': '18.0.1.0.0',
    'category': 'Accounting',
    'summary': 'Tree view for sales invoice lines with detailed product, payment, and customer info',
    'description': """
        Adds a comprehensive tree view for account.move.line
        filtered for out_invoice and out_refund with:
        - Branch, Channel, Sales Rep
        - Product Family, Vendor, UPC
        - Payment Journals info
        - Serial/Lot tracking
        - Signed amounts for credit notes
    """,
    'depends': [
        'account',
        'stock',
        'crm',
        'point_of_sale','accounting_customization',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/account_move_line_sales_view.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}

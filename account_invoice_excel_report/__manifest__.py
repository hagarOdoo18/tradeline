
{
    'name': 'Account Invoice Excel Report',
    'version': '18.0.1.1.0',
    'category': 'Accounting',
    'summary': 'Export invoices & payments to Excel (Odoo 18)',
    'depends': ['account','branch'],
    'data': [
        'security/ir.model.access.csv',
        'views/invoice_excel_wizard_view.xml',
        'views/account_invoice_wizard_view.xml',
    ],
    'installable': True,
}

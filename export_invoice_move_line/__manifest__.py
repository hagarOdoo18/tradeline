{
    'name': 'Export Invoice Move Line',
    'version': '1.0',
    'category': 'Accounting',
    'summary': 'Export invoice move lines filtered by branch, salesperson, and payment journal',
    'author': 'Ezzat',
    'depends': ['accountant', 'branch','base'],
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'views/export_invoice_move_line_wizard_view.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

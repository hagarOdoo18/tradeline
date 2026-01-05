{
    'name': 'Invoice Print Selected Lines',
    'version': '18.0.1.0.0',
    'depends': ['account'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/invoice_line_print_wizard_view.xml',
        'actions/invoice_line_print_wizard_action.xml',
        'views/account_move_form_inherit.xml',
    ],
    'installable': True,
}
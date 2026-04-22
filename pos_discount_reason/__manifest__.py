# __manifest__.py
{
    'name': 'POS Discount Reason',
    'version': '18.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Complete POS enhancements with discount reason, sales rep, and auto invoice',
    'description': '''
        This module adds:
        - Discount reason field on POS orders with button
        - As gift flag with button
        - Sales rep from HR employees with button
        - Auto invoice functionality (default enabled)
        - Auto print invoice functionality (default enabled)
    ''',
    'author': 'Your Name',
    'website': 'https://www.yourwebsite.com',
    'depends': ['point_of_sale', 'hr','base_tradeline','pos_hr'],
    'data': [
        'security/ir.model.access.csv',
        'views/discount_reason_views.xml',
        'views/pos_order_views.xml',
        'views/pos_config_views.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_discount_reason/static/src/js/popups.js',
            'pos_discount_reason/static/src/js/discount_lock.js',
            'pos_discount_reason/static/src/xml/control_buttons.xml',
            'pos_discount_reason/static/navbar/navbar.xml',
            'pos_discount_reason/static/src/js/payment_screen.js',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
}

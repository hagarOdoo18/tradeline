{
    'name': 'Sale Order Payment Return',
    'version': '18.0.1.0.0',
    'summary': 'Add Return Payment button on Sale Order to reverse linked payments',
    'description': """
        This module adds a "Return Payment" button on the Sale Order form.
        When clicked, it automatically reverses all posted payments linked
        to the sale order's invoices by creating refund/reverse journal entries.
    """,
    'author': 'Custom',
    'category': 'Sales',
    'depends': ['sale_management', 'account','branch'],
    'data': [
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}

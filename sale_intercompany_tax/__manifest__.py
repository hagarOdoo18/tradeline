# -*- coding: utf-8 -*-
{
    'name': 'Sale Inter-company Tax',
    'version': '18.0.1.0.0',
    'category': 'Sales',
    'summary': 'Apply different taxes on sale order lines when the customer belongs to a different company.',
    'description': """
Sale Inter-company Tax
======================
When a sale order is created for a customer that is linked to a different company
(inter-company partner), this module automatically applies the taxes configured on
that partner instead of the standard product taxes.

Features:
- Mark any partner as an inter-company customer manually or detect automatically.
- Configure dedicated taxes on the partner form (Inter-company Taxes tab).
- Taxes are applied automatically when the partner is selected on a sale order.
- Works with fiscal positions; inter-company taxes override after fiscal position mapping.
    """,
    'depends': ['sale'],
    'data': [
        'views/res_partner_view.xml',
        'views/sale_order_view.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

# -*- coding: utf-8 -*-
{
    'name': 'Change Sales Representative',
    'version': '18.0.1.0.0',
    'category': 'Sales',
    'summary': 'Bulk-reassign all sales records from one salesperson to another',
    'description': """
Change Sales Representative
===========================
A simple wizard with two fields — Old Sales Rep and New Sales Rep —
that bulk-updates every record currently assigned to the old user:

* Customer Invoices, Credit Notes, Vendor Bills, Vendor Refunds (account.move.invoice_user_id)
* Sale Orders (sale.order.user_id)
* POS Orders (pos.order.user_id)

A summary notification reports how many records were touched.
""",
    'author': 'Tradeline',
    'website': 'https://Tradeline.com',
    # NOTE: this module depends on a third-party module that provides the
    # `sales.rep` model and adds a `sales_rep_id` field to account.move,
    # sale.order, and pos.order. Replace 'sales_rep_module' below with the
    # actual technical name of that module in your codebase.
    'depends': [
        'account',
        'sale_management',
        'point_of_sale',
        # 'sales_rep_module',
    ],
    'data': [
        'security/ir.model.access.csv',
        'wizard/change_sales_rep_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

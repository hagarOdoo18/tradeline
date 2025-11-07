# -*- coding: utf-8 -*-
{
    'name': "Accounting customization",

    'summary': """
        custom Inventory customization""",

    'description': """
        Long description of module's purpose
    """,

    'author': "Ezzat",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base', 'account','base_tradeline','sale_management','stock','crm','sale_stock','sale'],

    # always loaded
    'data': [
        'security/security.xml',
        'views/account_move.xml',
        'views/sale_order.xml',
    ],
}
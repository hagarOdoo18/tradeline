# -*- coding: utf-8 -*-
{
    'name': "Inventory customization",

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
    'depends': ['base', 'product','stock','base_tradeline','stock_account','stock_landed_costs'],

    # always loaded
    'data': [
        'security/security.xml',
        'views/product.xml',
        'views/attribute.xml',
        'views/purchase.xml',
        'views/serial.xml',

    ],
}
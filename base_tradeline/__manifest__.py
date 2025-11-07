# -*- coding: utf-8 -*-
{
    'name': "Base Tradeline",

    'summary': """
        custom Base Models""",

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
    'depends': ['base', 'account','stock','purchase','point_of_sale'],

    # always loadedclarification
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/bank_details.xml',
        'views/channel.xml',
        'views/courier.xml',
        'views/discount_reason.xml',
        'views/sales_rep.xml',
        'views/product_warranty.xml',
        'views/product_family.xml',
        'views/pos_config.xml',
        'views/lot.xml',
        'views/sub_category.xml',
        'views/menus.xml',
    ],
}
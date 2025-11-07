# -*- coding: utf-8 -*-
{
    'name': "Customization Prodcut Code",



    'author': "Tradeline",
    'website': "http://www.Tradelinestores.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base','inventory_customization'],

    # always loaded
    'data': [
        'security/security.xml',
        'data/data.xml',
        'views/product.xml',
    ],

}
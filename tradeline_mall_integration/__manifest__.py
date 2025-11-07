# -*- coding: utf-8 -*-
{
    'name': "Mall Integration",



    'author': "Tradeline",
    'website': "http://www.Tradelinestores.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base','account','branch'],

    # always loaded
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/config_data.xml',
        'views/config_day.xml',
        'views/config_day_line.xml',
        'views/config_month.xml',
        'views/send_day.xml',
        'views/base_integration.xml',
        'views/menu_items.xml',
    ],

}
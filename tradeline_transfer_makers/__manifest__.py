# -*- coding: utf-8 -*-
{
    'name': "Customization Transfer Makers",



    'author': "Tradeline",
    'website': "http://www.Tradelinestores.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['stock','base_tradeline'],

    # always loaded
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/transfer_makers.xml',
        'views/stock_picking.xml',
    ],

}
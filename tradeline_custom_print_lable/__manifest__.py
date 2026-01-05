# -*- coding: utf-8 -*-
{
    'name': "tradeline custom Print Lable",



    'author': "Tradeline",
    'website': "http://www.tradelinestores.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['stock'],

    # always loaded
    'data': [


        'report/invoice_new_report_template.xml',
    ],
    # only loaded in demonstration mode,

}
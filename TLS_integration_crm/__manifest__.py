# -*- coding: utf-8 -*-
{
    'name': "TLS Integration CRM",



    'author': "Tradeline",
    'website': "http://www.TradelineStore.com",

    # Categories can be used to filter modules in modules listing
    # Checktradeline_groups_view_sale_order https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base','account_accountant','account','sale'],

    # always loaded
    'data' : [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/tvc_setting.xml',
        'views/inherited_account_journal.xml',
        'views/tvc.xml',
        'views/cron.xml',
    ],
}

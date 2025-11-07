# -*- coding: utf-8 -*-
{
    'name': "tradeline_crm_customize",
    'author': "Centione",
    'website': "http://www.centione.com",
    'category': 'CRM',
    'version': '0.1',
    'depends': ['base', 'crm', 'account', 'sale','sale_crm'],
    'data': [
        'data/email_to_branch_data.xml',
        'security/security.xml',
        'security/ir.model.access.csv',
        'wizard/cancel_sale_order_views.xml',
        'views/customers_views.xml',
        'wizard/crm_lead_to_opportunity_views.xml',
        'views/views.xml',
        'views/pipeline_views.xml',

        'views/sequence.xml',

        # 'views/kanban_edit_setting_general.xml',
    ],
# 'qweb': [
#         "static/src/xml/kanban_edit_setting_general.xml",
#     ],
}

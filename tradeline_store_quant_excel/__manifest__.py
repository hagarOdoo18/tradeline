# -*- coding: utf-8 -*-
{
    'name': "Tradeline Stock Quant Excel",
    'summary': "On-hand stock quantity per product across internal store locations (Excel)",
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'depends': [
        'stock',
        'account',
        'inventory_customization',   # product.template.family_id / sub_categ_id (+ base_tradeline)
    ],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/views.xml',
    ],
    'installable': True,
    'application': False,
}

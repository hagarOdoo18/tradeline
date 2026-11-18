# -*- coding: utf-8 -*-
{
    'name': "Tradeline QTY Des Excel",
    'summary': "Store product on-hand & sold (POS + Sale) matrix Excel report",
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'depends': [
        'stock',
        'point_of_sale',
        'sale_management',
        'inventory_customization',  # product.template.family_id (+ base_tradeline product.family)
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/views.xml',
    ],
    'installable': True,
    'application': False,
}

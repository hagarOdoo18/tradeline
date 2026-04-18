# -*- coding: utf-8 -*-
{
    'name': "Tradeline Stock Scrap Custom",
    'summary': "Custom stock scrap workflow with approval",
    'author': "Tradelines",
    'website': "http://www.tradelines.com",
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'category': 'Inventory/Inventory',
    'depends': [
        'stock',
        'point_of_sale',

    ],
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'views/views.xml',
        'views/scrap.xml',
    ],
    'installable': True,
    'application': False,
}

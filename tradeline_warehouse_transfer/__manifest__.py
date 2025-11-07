# -*- coding: utf-8 -*-
{
    'name': "Tradeline Transfer Request",

    'summary': """
        Tradeline Transfer Request""",

    'author': "Tradeline",
    'website': "http://www.Tradeline.com",


    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base','stock','account','base_tradeline'],

    # always loaded
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        # 'views/configuration.xml',
        'views/cancel_reason.xml',
        'views/transfer_request.xml',
        'views/extra_qty_request.xml',
        'views/data.xml',
        'views/report.xml',
        'views/picking.xml',
    ],
}
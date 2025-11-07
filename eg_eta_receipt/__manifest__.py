# -*- coding: utf-8 -*-
{
    'name': "EG ETA Receipt",

    'summary': """ Egyptian e-receipt """,

    'description': """
        Egyptian e-receipt 
    """,

    'author': "iSky Development",
    'website': "https://sdk.invoicing.eta.gov.eg",
    'category': 'accounting',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base','point_of_sale','l10n_eg_edi_eta'],

    # always loaded
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'views/pos_config.xml',
        # 'views/pos_order.xml',
        'views/pos_order_view_changes.xml',
        'data/data.xml'

    ],

'assets': {
        'point_of_sale.assets': [
            'eg_eta_receipt/static/src/js/pos_payment_screen.js',
             'eg_eta_receipt/static/src/js/pos_order.js',
             'eg_eta_receipt/static/src/js/models.js',
            'eg_eta_receipt/static/src/js//qrcode.js'
        ],
    'web.assets_qweb': [
        'eg_eta_receipt/static/src/xml/pos.xml',
    ],


    }

}

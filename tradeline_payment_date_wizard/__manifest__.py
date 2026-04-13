# -*- coding: utf-8 -*-
{
    'name': "Tradeline Payment Date Wizard",
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'depends': ['base', 'account'],

    # always loaded
    'data': [
        'views/date_wizard.xml',
        'security/ir.model.access.csv',

    ],

}
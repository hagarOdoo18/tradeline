# -*- coding: utf-8 -*-

{
    'name': 'BioTime Integration',
    'version': '18.0',
    'summary': 'BIO',
    'description' :"""
       BIO
    """,
    'depends': ['base','hr','hr_attendance'],
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'views/biotime.xml',
        'views/terminal.xml',
        'views/biotime_transaction.xml',
        'views/employee.xml',
        'data/menuitems.xml',
        'data/cron.xml'

    ],
    "license": "LGPL-3",
    'installable': True,
    'auto_install': False,

}

# -*- coding: utf-8 -*-
{
    'name': 'Fix Blocked Receipts — Duplicate Serial Numbers',
    'version': '18.0.1.0.0',
    'category': 'Inventory',
    'summary': (
        'Cron job that finds receipts blocked by duplicate serial numbers '
        'and fixes the move lines so they can be validated.'
    ),
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

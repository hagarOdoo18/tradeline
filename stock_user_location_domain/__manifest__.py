{
    'name': 'Stock User Location Domain',
    'version': '1.0',
    'summary': 'Restrict visible stock locations by user',
    'description': 'This module limits stock locations shown in pickings to those assigned to the current user.',
    'author': 'Ezzat',
    'depends': ['stock'],
    'data': [
        'views/res_users_view.xml',
        # 'views/stock_picking_view.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

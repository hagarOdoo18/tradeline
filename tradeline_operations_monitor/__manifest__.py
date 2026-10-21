{
    'name': 'Tradeline Operations Monitor',
    'summary': 'Early warnings for Shopify delays, stock jobs, database locks and email delivery',
    'version': '18.0.1.0.2',
    'license': 'LGPL-3',
    'depends': ['mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/monitor_data.xml',
        'views/monitor_views.xml',
    ],
    'installable': True,
    'application': True,
}

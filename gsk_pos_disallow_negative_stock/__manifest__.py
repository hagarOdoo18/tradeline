{
    'name': 'POS Prevent Negative Stock',
    'version': '1.0.0',
    'summary': 'Block POS Sales that results in negative stock',
    'category': 'Sales/Point of Sale',
    'author': 'Gritnec Solutions',
    'website': 'https://gritnecsolutions.com',
    'license': 'OPL-1',
    'depends': ['point_of_sale'],
    'assets':{
        'point_of_sale.assets':[
            'gsk_pos_disallow_negative_stock/static/src/css/styles.scss',
            'gsk_pos_disallow_negative_stock/static/src/js/StockPopup.js',
            'gsk_pos_disallow_negative_stock/static/src/js/PaymentScreen.js',
            'gsk_pos_disallow_negative_stock/static/src/xml/*.xml',
        ],
    },
    'installable': True,
    'auto_install': False,

    'price': '33',
    'currency': 'USD',
    'images': ['static/description/banner.gif']
}
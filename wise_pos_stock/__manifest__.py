# -*- coding: utf-8 -*-
{
    'name': "POS Stock",
    'summary': "The Odoo POS Stock module integrates seamlessly with the Point of Sale system, displaying real-time product quantities and facilitating efficient inventory management without leaving the POS interface.[ POS Stock | pos inventory management | stock control | POS product stock | Show stock pos | POS Order stock | pos product qty | point of sale stock | POS Mobile | POS Product Warehouse Quantity ]",
    'description': """Effortlessly monitor inventory levels directly from the POS screen, with real-time product quantity display. Low stock items are conveniently shown on a separate screen, allowing for easy identification and prompt action. Prevent sales of out-of-stock items and streamline operations for enhanced efficiency and informed decision-making
    
    Key features include:
    
    Real-Time Stock Display

    View product quantities in real-time according to Odoo’s default update options, providing up-to-date inventory information directly on the POS interface.
    Stock Location Selection

    Configure stock locations for sessions directly from the POS, ensuring precise and efficient inventory management for each session.
    Out-of-Stock Management

    Configure settings to disable product selection for out-of-stock items, ensuring customers are promptly informed about product availability.
    Low Stock Screen

    Select the type of stock to be displayed on the POS interface, with options for On Hand and Forecasted stock levels, providing flexibility in how inventory information is presented.

    Color Customization

    Tailor low stock and in-stock item indicators with personalized color options, ensuring efficient inventory management and visual clarity on the POS interface.
    
    [ POS Stock | pos inventory management | stock control | POS product stock | Show stock pos | POS Order stock | pos product qty | point of sale stock | POS Product Warehouse Quantity | pos warehouse ]
    """,
    'author': "Wisenetic",
    'website': "https://www.wisenetic.com",
    "support": "info@wisenetic.com",
    'category': "Sales/Point of Sale",
    'version': "18.0.0.1",
    'depends': ['point_of_sale'],
    'data': ["views/res_config_settings_views.xml"],
    'assets': {
        'point_of_sale._assets_pos': [
            'wise_pos_stock/static/src/**/*',
        ]
    },
    'images': ['static/description/banner.gif'],
    # 'live_test_url': 'https://youtu.be/yXCQkNlO5_8',
    'installable': True,
    'auto_install': False,
    'application': True,
    'license': 'OPL-1',
    'price': '18.58',
    'currency': 'USD'
}

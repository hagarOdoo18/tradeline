# -*- coding: utf-8 -*-
{
    'name': 'Allow Sell Below Cost (Sale)',
    'version': '18.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Centralized setting to allow selling below cost price with date range control',
    'description': """
        This module adds a centralized configuration to allow selling products below
        their cost price in both Sales Orders and Point of Sale.
        
        Features:
        - Enable/disable selling below cost globally from Settings
        - Define a From/To date range during which selling below cost is permitted
        - Warning or block in Sales Order lines when price < cost
        - Warning or block in POS order lines when price < cost
        - Fully integrated with Odoo 18 Settings page
    """,
    'author': 'Tradeline',
    'depends': [
        'sale_management',
        'point_of_sale',
        'base_setup',
    ],
    'data': [

        'views/product_product.xml',
        'views/sale_order_views.xml',
    ],

    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}

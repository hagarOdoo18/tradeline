# -*- coding: utf-8 -*-
{
    'name': 'Stock Valuation Layer - Category, Family & Vendor',
    'version': '18.0.1.0.0',
    'summary': 'Add Item Code, Category, Family, Vendor, Last PO Cost, Available Qty to Stock Valuation Layer',
    'category': 'Inventory/Valuation',
    'author': 'Custom',
    'depends': ['stock_account', 'purchase'],
    'data': [
        'views/stock_valuation_layer_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

# -*- coding: utf-8 -*-
{
    'name': "Tradeline Stock Quant Sold Excel",
    'summary': "Quantity sold per product by sales team (invoices + SRO orders) Excel report",
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'depends': [
        'stock',
        'account',
        'sale_management',
        'accounting_customization',   # sale.order/account.move inv_type ('sro'), team_id on move
        'inventory_customization',    # product.template.family_id (+ base_tradeline product.family)
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/views.xml',
    ],
    'installable': True,
    'application': False,
}

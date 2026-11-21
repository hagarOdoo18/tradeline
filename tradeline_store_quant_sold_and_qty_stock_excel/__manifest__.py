# -*- coding: utf-8 -*-
{
    'name': "Tradeline Stock Quant Sold and Stock QTY Excel",
    'summary': "Sold qty + on-hand stock qty per product by branch (invoices + SRO) Excel report",
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'depends': [
        'stock',
        'account',
        'sale_management',
        'branch',                     # res.branch; branch_id on account.move(.line), stock.quant, sale.order
        'accounting_customization',   # sale.order inv_type ('sro')
        'inventory_customization',    # product.template.family_id (+ base_tradeline product.family)
    ],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/views.xml',
    ],
    'installable': True,
    'application': False,
}

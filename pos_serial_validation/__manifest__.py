# -*- coding: utf-8 -*-
{
    'name': 'POS Serial Validation',
    'version': '18.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'التحقق من الأرقام التسلسلية في نقطة البيع',
    'description': """
        موديل للتحقق من صحة المنتجات والأرقام التسلسلية في مخزن POS:
        - التحقق من وجود Serial في المخزن
        - منع تكرار Serial
        - التحقق عند البيع في POS
    """,
    'author': 'Custom',
    'depends': ['point_of_sale', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/pos_serial_validation_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

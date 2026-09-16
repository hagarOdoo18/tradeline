{
    'name': 'Pricelist Barcode Label Print',
    'version': '18.0.1.0.0',
    'summary': 'Print barcode labels from pricelist lines with custom quantity',
    'category': 'Sales/Sales',
    'depends': ['product', 'sale', 'web'],
    'data': [
        'security/ir.model.access.csv',
        'report/pricelist_barcode_label_report.xml',
        'views/pricelist_barcode_wizard_view.xml',
        'views/product_pricelist_view.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

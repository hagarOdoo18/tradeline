{
    'name': 'Tradeline Local VAT Existing Action Guard',
    'version': '18.0.1.0.2',
    'license': 'LGPL-3',
    'summary': 'Preserve ETA history when using the existing Apply 14% Tax action',
    'depends': ['accounting_customization', 'tradeline_eta_reliability'],
    'data': ['data/existing_vat_action.xml'],
    'installable': True,
    'auto_install': True,
}

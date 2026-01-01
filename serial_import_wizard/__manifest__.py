{
    'name': 'Serial Import Wizard',
    'version': '1.0',
    'summary': 'Import Serials from Wizard with custom naming',
    'category': 'Inventory',
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',

        'views/serial_import_wizard_view.xml',
    ],
    'installable': True,
}

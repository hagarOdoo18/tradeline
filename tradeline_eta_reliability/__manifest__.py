{
    'name': 'Tradeline ETA Reliable Submission',
    'version': '18.0.1.0.33',
    'license': 'LGPL-3',
    'depends': ['egyptian_electronic_invoice', 'l10n_eg_edi_eta', 'tradeline_operations_monitor'],
    'data': ['security/security.xml', 'security/ir.model.access.csv', 'data/cron.xml',
             'views/eta_job.xml', 'views/operations_monitor.xml', 'views/account_move.xml', 'views/eta_workspace.xml'],
    'assets': {'web.assets_backend': ['tradeline_eta_reliability/static/src/js/staff_operation.js',
        'tradeline_eta_reliability/static/src/js/connection_health.js',
        'tradeline_eta_reliability/static/src/xml/connection_health.xml']},
    'installable': True,
    'auto_install': True,
}

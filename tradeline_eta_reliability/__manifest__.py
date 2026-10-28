{
    'name': 'Tradeline ETA Reliable Submission',
    'version': '18.0.1.0.6',
    'license': 'LGPL-3',
    'depends': ['egyptian_electronic_invoice', 'l10n_eg_edi_eta', 'tradeline_operations_monitor'],
    'data': ['security/security.xml', 'security/ir.model.access.csv', 'data/cron.xml',
             'views/eta_job.xml', 'views/operations_monitor.xml'],
    'installable': True,
    'auto_install': True,
}

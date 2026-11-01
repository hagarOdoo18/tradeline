from odoo import api, SUPERUSER_ID

def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['tradeline.eta.job']._refresh_blocked_staff_rejection_labels()

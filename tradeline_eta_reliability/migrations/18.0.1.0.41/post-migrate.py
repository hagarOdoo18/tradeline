from odoo import api, SUPERUSER_ID

def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['tradeline.eta.job']._refresh_review_staff_labels()

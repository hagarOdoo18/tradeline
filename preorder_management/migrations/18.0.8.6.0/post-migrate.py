from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["sale.preorder"].search(
        [("state", "not in", ("completed", "cancelled"))]
    ).migrate_payments_to_delivery()

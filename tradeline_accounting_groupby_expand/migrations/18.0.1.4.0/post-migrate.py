from odoo import SUPERUSER_ID, api
from odoo.addons.tradeline_accounting_groupby_expand import hooks


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    hooks.post_init_hook(env)

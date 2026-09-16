from odoo import models, fields, api, exceptions
from datetime import datetime


class POSConfig(models.Model):
    _inherit = 'pos.config'

    deviceSerialNumber = fields.Char(string="", required=False, )
    activityCode = fields.Char(string="", required=False, )
    branch = fields.Char(string="", required=False, )
    require_customer = fields.Float(string="Receipt thresholds", required=False, )
    l10n_eg_client_identifier = fields.Char('ETA Client ID')
    l10n_eg_client_secret = fields.Char('ETA Secret')
    l10n_eg_production_env = fields.Boolean('In Production Environment')

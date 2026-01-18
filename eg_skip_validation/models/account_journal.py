from odoo import models, fields

class AccountJournal(models.Model):
    _inherit = 'account.journal'

    l10n_eg_skip_eta_validation = fields.Boolean(
        string="Skip ETA Validation",
        help="If enabled, ETA validation checks will be skipped for this journal."
    )

"""Load after both modules without modifying the original accounting action."""
from odoo import models


class AccountMoveLocalVatActionBridge(models.Model):
    _inherit = 'account.move'

    def action_apply_14_tax(self):
        if self.filtered(lambda move: move._eta_apply14_requires_review()):
            return self.action_eta_apply_local_vat()
        return super().action_apply_14_tax()

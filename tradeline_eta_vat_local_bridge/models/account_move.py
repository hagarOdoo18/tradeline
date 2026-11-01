"""Load after both modules without modifying the original accounting action."""
from odoo import models, _
from odoo.exceptions import UserError


class AccountMoveLocalVatActionBridge(models.Model):
    _inherit = 'account.move'

    def action_apply_14_tax(self):
        if self.filtered(lambda move: move._eta_local_vat_has_history()):
            if len(self) != 1:
                raise UserError(_('Select one previously sent invoice at a time to preview its local VAT correction.'))
            return self.action_eta_local_vat_preview()
        return super().action_apply_14_tax()

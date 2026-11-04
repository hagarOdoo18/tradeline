from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class StockLot(models.Model):
    _inherit = 'stock.lot'

    verify_code = fields.Integer(string='Verify Code', copy=False, index=True)

    @api.constrains('verify_code')
    def _check_verify_code_unique(self):
        for lot in self:
            # An unset code (0 / empty) is not constrained; many lots have none.
            if not lot.verify_code:
                continue
            # sudo() so the code is unique across companies, not only the user's.
            duplicate = self.sudo().search_count([
                ('id', '!=', lot.id),
                ('verify_code', '=', lot.verify_code),
            ])
            if duplicate:
                raise ValidationError(_(
                    'Verify Code %(code)s is already used by another lot/serial; '
                    'it must be unique.', code=lot.verify_code))

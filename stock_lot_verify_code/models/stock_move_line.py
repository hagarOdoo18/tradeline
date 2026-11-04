from odoo import fields, models


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    verify_code = fields.Char(
        string='Verify Code',
        related='lot_id.verify_code',
        readonly=True,
    )

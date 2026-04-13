from odoo import fields, models


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    user_ids = fields.Many2many(
        'res.users',
        'stock_picking_type_user_rel',
        'picking_type_id',
        'user_id',
        string='Allowed Users',
        help='Only these users can select this operation type on transfers.',
    )

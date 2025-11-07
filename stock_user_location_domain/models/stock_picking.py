from odoo import models, api,fields

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    location_dest_id = fields.Many2one(
        'stock.location',
        string="Destination Location",
        domain=lambda self: self._get_user_location_domain(),
    )

    @api.model
    def _get_user_location_domain(self):
        """Return domain allowed for current user"""
        user = self.env.user
        loc_ids = user.stock_location_ids.ids or []
        return [('id', 'in', loc_ids)] if loc_ids else []

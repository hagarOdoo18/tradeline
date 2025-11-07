from odoo import models, fields

class ResUsers(models.Model):
    _inherit = 'res.users'

    stock_location_ids = fields.Many2many(
        'stock.location',
        string='Allowed Locations',
        help='User can only access pickings with these stock locations.'
    )

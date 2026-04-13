from odoo import fields, models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    scrap_vendor_location = fields.Boolean(string='Vendor Scrap Location')

from odoo import fields, models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    scrap_vendor_location = fields.Boolean(
        string='Is a Scrap Vendor Location?',
        default=False,
        help='Check this box to allow using this location to put scrapped/damaged goods.',
    )

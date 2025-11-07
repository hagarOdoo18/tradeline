from odoo import fields, models, api

class StockLot(models.Model):
    _inherit = 'stock.lot'

    item_code = fields.Char(
        string='Item code',related="product_id.barcode",
        required=False)
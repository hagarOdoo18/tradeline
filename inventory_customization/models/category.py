from odoo import fields, models, api

class ProductCategory(models.Model):
    _inherit = 'product.category'

    max_discount = fields.Float(
        string='Max Discount',
        required=False)

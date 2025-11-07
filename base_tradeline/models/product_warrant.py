from odoo import fields, models, api


class ProductWarrant(models.Model):
    _name = 'product.warranty'
    _description = 'Product Warranty'

    name = fields.Char(required=True)
    
    categ_ids = fields.Many2many(
        comodel_name='product.category',
        string='Product Categories')

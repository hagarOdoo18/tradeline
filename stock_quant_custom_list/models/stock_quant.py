from odoo import models, fields, api

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    # Related fields from product (product.product -> product_tmpl)
    family_id = fields.Many2one(
        'product.family',
        string='Family',
        related='product_id.product_tmpl_id.family_id',
        store=True,
        readonly=True
    )
    category_id = fields.Many2one(
        'product.category',
        string='Category',
        related='product_id.categ_id',
        store=True,
        readonly=True
    )
    sub_categ_id = fields.Many2one(
        'sub.category',
        string='Sub Category',
        related='product_id.product_tmpl_id.sub_categ_id',
        store=True,
        readonly=True
    )
    default_code = fields.Char(
        string='Item Code',
        related='product_id.barcode',
        store=True,
        readonly=True
    )

    vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        related='product_id.vendor_id',
        store=True,
        readonly=True
    )


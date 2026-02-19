from odoo import models, fields, api


class ProductProduct(models.Model):
    _inherit = 'product.product'

    variant_name = fields.Char(
        string='Variant Name',
        compute='_compute_variant_name',
        store=True,
        help='Combined attribute values name for search purposes.',
    )

    @api.depends('product_template_variant_value_ids', 'product_template_variant_value_ids.name')
    def _compute_variant_name(self):
        for product in self:
            product.variant_name = '%s %s' % (
                product.name,
                ', '.join(product.product_template_variant_value_ids.mapped('name'))
            ) if product.product_template_variant_value_ids else product.name or ''
from odoo import fields, models, api

class ProductAttribute(models.Model):
    _inherit = 'product.attribute'

    is_vendor = fields.Boolean(
        string='Is Vendor Option',
        required=False)

class ProductAttributeValue(models.Model):
    _inherit = 'product.attribute.value'

    vendor_id = fields.Many2one(
        comodel_name='res.partner',
        string='Vendor',domain=[('vendor','=',True)],
        required=False)


    @api.onchange('vendor_id')
    def _onchange_vendor_id(self):
        self.name = self.vendor_id.name
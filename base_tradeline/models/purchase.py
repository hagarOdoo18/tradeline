from odoo import fields, models, api


from odoo.exceptions import UserError



class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'


    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id and not self.order_id.partner_id:
            raise UserError(('Please Set Vendor'))


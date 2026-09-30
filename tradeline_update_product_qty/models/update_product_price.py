from odoo import fields, models, api,_

from odoo.exceptions import ValidationError
import xlsxwriter
from io import BytesIO
import base64
from tempfile import TemporaryFile
import openpyxl


class UpdateProductPrice (models.TransientModel) :
    _name = 'update.product.qty.wizard'
    _description = "Update Product Qty Wizard"

    def default_stock(self):
        return self.env['stock.location'].search([('usage', '=', 'internal'),('company_id','=',self.env.company.id)]).ids




    company_id = fields.Many2one('res.company', 'Company', default=lambda self: self.env.company.id)

    stock_ids = fields.Many2many(
        comodel_name='stock.location',
        string='Stocks',domain="[('usage', '=', 'internal'),('company_id','=',company_id)]",default=default_stock,
        required=True)


    product_ids = fields.Many2many(
        comodel_name='product.product', required=True,
        string='Products')

    qty = fields.Integer(
        string='Qty',
        required=True)


    def create_adjust(self):
        self.ensure_one()
        if self.qty < 0:
            raise ValidationError(_('Counted quantity cannot be negative.'))
        for product in self.product_ids.sorted('id'):
            product._lock_inventory_adjustment(self.company_id)
        for stock in self.stock_ids:
            if stock.usage != 'internal' or (stock.company_id and stock.company_id != self.company_id):
                raise ValidationError(_('Select an internal location for the chosen company.'))
            for product in self.product_ids:
                if product.tracking != 'none':
                    raise ValidationError(_(
                        'Use a lot/serial inventory adjustment for %(product)s.',
                        product=product.display_name,
                    ))
                product._apply_counted_inventory(
                    self.company_id, stock, self.qty, reason=_('Update Product Quantity: physical count'))

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




    company_id = fields.Many2one('res.company', 'Company', default=lambda self: self.env.user.company_id.id)

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
        for stock  in self.stock_ids:
            if stock.usage != 'internal' or (stock.company_id and stock.company_id != self.company_id):
                raise ValidationError(_('Select an internal location for the chosen company.'))
            for product in self.product_ids:
                if product.tracking != 'none':
                    raise ValidationError(_(
                        'Use a lot/serial inventory adjustment for %(product)s.',
                        product=product.display_name,
                    ))
                company_product = product.with_company(self.company_id)
                if (company_product.categ_id.property_cost_method == 'average' and
                        company_product.standard_price < 0):
                    raise ValidationError(_(
                        '%(product)s has a negative AVCO Product Cost. Reconcile its valuation before adjusting stock.',
                        product=product.display_name,
                    ))
                inventory_quant = self.env['stock.quant'].with_company(self.company_id).search([
                    ('location_id', '=', stock.id),
                    ('product_id', '=', product.id),
                    ('company_id', '=', self.company_id.id),
                ])
                if len(inventory_quant) > 1:
                    raise ValidationError(_(
                        'Multiple stock lines exist for %(product)s at %(location)s. Adjust them individually.',
                        product=product.display_name,
                        location=stock.display_name,
                    ))
                if not inventory_quant:
                    inventory_quant = self.env['stock.quant'].with_company(self.company_id).with_context(inventory_mode=True).create({
                        'location_id': stock.id,
                        'branch_id': stock.branch_id.id,
                        'product_id': product.id,
                        'company_id' :  self.company_id.id,
                        'inventory_quantity': self.qty,
                    })
                else:
                    inventory_quant.inventory_quantity = self.qty
                # Applying the count creates the stock move and valuation layer.
                result = inventory_quant.action_apply_inventory()
                if result:
                    raise ValidationError(_(
                        'Inventory adjustment for %(product)s at %(location)s needs review.',
                        product=product.display_name,
                        location=stock.display_name,
                    ))


















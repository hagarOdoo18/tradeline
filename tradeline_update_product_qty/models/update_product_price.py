from odoo import fields, models, api,_

from odoo.exceptions import ValidationError
import xlsxwriter
from io import BytesIO
import base64
from tempfile import TemporaryFile
import openpyxl


class UpdateProductPrice (models.TransientModel) :
    _name = 'update.product.qty.wizard'

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
        for stock  in self.stock_ids:
            for product in self.product_ids:
                inventory_quant = self.env['stock.quant'].search([
                    ('location_id', '=', stock.id),
                    ('product_id', '=', product.id),
                ])
                if not inventory_quant:
                    adjust = self.env['stock.quant'].with_context(inventory_mode=True).create({
                        'location_id': stock.id,
                        'branch_id': stock.branch_id.id,
                        'product_id': product.id,
                        'company_id' :  self.company_id.id,
                        'quantity': self.qty,
                    })
                    self.env.company= self.company_id.id
                    adjust.action_set_inventory_quantity()
                    adjust.action_apply_inventory()
                else:
                    inventory_quant.quantity = self.qty

















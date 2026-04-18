from odoo import fields, models


class ScrapLine(models.TransientModel):
    _name = 'scrap.line'
    _description = 'Scrap Line'

    product_id = fields.Many2one(
        comodel_name='product.product', string='Product', store=True)

    quantity = fields.Integer(string='Quantity')

    lot_id = fields.Many2one(
        comodel_name='stock.lot',
        string='Lot & Serial', store=True)

    scrap_id = fields.Many2one(
        comodel_name='stock.scrap.wizard',
        string='Scrap')

    product_uom_id = fields.Many2one(
        'uom.uom', string='Unit of Measure', store=True)

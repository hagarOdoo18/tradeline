# -*- coding: utf-8 -*-
from odoo import fields, models


class InventoryCountCostLog(models.Model):
    _name = 'stock.count.cost.log'
    _description = 'Controlled Inventory Count and Cost Adjustment'
    _order = 'id desc'

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True)
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    location_id = fields.Many2one('stock.location', required=True, readonly=True)
    lot_id = fields.Many2one('stock.lot', readonly=True)
    user_id = fields.Many2one('res.users', required=True, readonly=True)
    performed_at = fields.Datetime(required=True, readonly=True)
    reason = fields.Text(required=True, readonly=True)
    cost_source = fields.Char(readonly=True)
    previous_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    counted_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    quantity_difference = fields.Float(readonly=True, digits='Product Unit of Measure')
    previous_product_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    final_product_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    previous_product_value = fields.Monetary(readonly=True, currency_field='currency_id')
    final_product_value = fields.Monetary(readonly=True, currency_field='currency_id')
    previous_product_cost = fields.Float(readonly=True, digits='Product Price')
    final_product_cost = fields.Float(readonly=True, digits='Product Price')
    target_unit_cost = fields.Float(readonly=True, digits='Product Price')
    cost_changed = fields.Boolean(readonly=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True)
    valuation_layer_ids = fields.Many2many('stock.valuation.layer', readonly=True)

    def action_view_valuation_layers(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Valuation Entries',
            'res_model': 'stock.valuation.layer',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.valuation_layer_ids.ids)],
        }

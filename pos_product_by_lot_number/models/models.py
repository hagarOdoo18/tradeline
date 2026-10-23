# -*- coding: utf-8 -*-
#################################################################################
#
#   Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>)
#   See LICENSE file for full copyright and licensing details.
#   License URL : <https://store.webkul.com/license.html/>
#
#################################################################################

from odoo import api, fields, models

LOT_QUANTITY_BATCH_SIZE = 10000

class StockProductionLot(models.Model):
    _name = 'stock.lot'
    _description = "Stock Lot"
    _inherit = ['stock.lot', 'pos.load.mixin']
    
    @api.model
    def check_lot_by_rpc(self, data):
        lot = self.search([("name","=",data.get("name")),("product_id","=",data.get("product_id"))])
        if lot:
            return True

    @api.model
    def _load_pos_data_domain(self, data):
        return []
        
    @api.model
    def _load_pos_data_fields(self, config_id):
        return ['name', 'product_id', 'product_qty', 'id']

    def _load_pos_data(self, data):
        """Keep the complete offline catalogue without caching every stock quant.

        Match stock.lot._product_qty: internal stock and company-owned transit
        stock, including zero/negative stock and reserved quantities. No sudo,
        location restriction or result limit is introduced here.
        """
        domain = self._load_pos_data_domain(data)
        field_names = self._load_pos_data_fields(data['pos.config']['data'][0]['id'])
        if domain is False or 'product_qty' not in field_names:
            return super()._load_pos_data(data)

        rows = self.search_read(domain, [name for name in field_names if name != 'product_qty'], load=False)
        Quant = self.env['stock.quant']
        for offset in range(0, len(rows), LOT_QUANTITY_BATCH_SIZE):
            batch = rows[offset:offset + LOT_QUANTITY_BATCH_SIZE]
            quantities = {
                lot.id: quantity
                for lot, quantity in Quant._read_group(
                    [
                        ('lot_id', 'in', [row['id'] for row in batch]),
                        '|', ('location_id.usage', '=', 'internal'),
                        '&', ('location_id.usage', '=', 'transit'),
                        ('location_id.company_id', '!=', False),
                    ],
                    ['lot_id'], ['quantity:sum'],
                )
            }
            for row in batch:
                row['product_qty'] = quantities.get(row['id'], 0.0)
        return {'data': rows, 'fields': field_names}

class EnableSettings(models.TransientModel):
    _inherit = "res.config.settings"

    @api.model
    def enable_lot_setting(self):
        enable_setting = self.create(dict(group_stock_production_lot = True))
        enable_setting.execute()

class ActionValidateInventory(models.Model):
    _inherit = "stock.quant"

    @api.model
    def validate_inventory(self):
        inventory = self.env.ref('pos_product_by_lot_number.stock_inventory_demo')
        validate = inventory.action_validate()

class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def _load_pos_data_models(self, config_id):
        models = super()._load_pos_data_models(config_id)
        models += ['stock.lot']
        return models

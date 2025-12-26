# -*- coding: utf-8 -*-
#################################################################################
#
#   Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>)
#   See LICENSE file for full copyright and licensing details.
#   License URL : <https://store.webkul.com/license.html/>
#
#################################################################################

from odoo import api, fields, models
class StockProductionLot(models.Model):
    _name = 'stock.lot'
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

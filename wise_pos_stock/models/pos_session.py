# -*- coding: utf-8 -*-
# Copyright (C) Wisenetic Technologies.

from odoo import models
from odoo import api, fields



class PosProduct(models.Model):
    _inherit = 'product.product'

    qty_available = fields.Float(string="On Hand Quantity")
    virtual_available = fields.Float(string="Forecasted Quantity")

    @api.model
    def _load_pos_data_fields(self, config_id):
        params = super()._load_pos_data_fields(config_id)
        print(params,"............params")
        params += ['qty_available', 'virtual_available']
        return params






class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def _load_pos_data_models(self, config_id):
        data = super()._load_pos_data_models(config_id)
        data += ['stock.location']
        return data



class Location(models.Model):
    _inherit = 'stock.location'

    @api.model
    def _load_pos_data_domain(self, data):
        return []

    @api.model
    def _load_pos_data_fields(self, config_id):
        return ['id', 'name']

    def _load_pos_data(self, data):
        domain = self._load_pos_data_domain(data)
        fields = self._load_pos_data_fields(data['pos.config']['data'][0]['id'])
        locations = self.search_read(domain, fields, load=False)
        return {
            'data': locations,
            'fields': fields,
        }



class PickingType(models.Model):
    _inherit = 'stock.picking.type'

    @api.model
    def _load_pos_data_fields(self, config_id):
        return super()._load_pos_data_fields(config_id)+['default_location_src_id']




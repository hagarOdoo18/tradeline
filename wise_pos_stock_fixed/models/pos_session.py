# -*- coding: utf-8 -*-
# Copyright (C) Wisenetic Technologies.

from odoo import models
from odoo import api, fields


class PosProduct(models.Model):
    _inherit = 'product.product'

    # DO NOT re-declare qty_available / virtual_available here.
    # Those are computed fields on product.product that respect the
    # `location` context key. Re-declaring them as plain Float fields
    # breaks that mechanism and always returns 0 / ignores location.

    @api.model
    def _load_pos_data_fields(self, config_id):
        params = super()._load_pos_data_fields(config_id)
        params += ['qty_available', 'virtual_available']
        return params

    def _load_pos_data(self, data):
        """
        Inject location context when the POS config is set to 'current'
        warehouse so the initial stock load already reflects the session's
        source location — not all warehouses combined.
        """
        config_data = data.get('pos.config', {}).get('data', [{}])[0]
        config_id = config_data.get('id')

        ctx = dict(self.env.context)
        if config_id:
            config = self.env['pos.config'].browse(config_id)
            if config.stock_warehouse == 'current':
                location = config.picking_type_id.default_location_src_id
                if location:
                    ctx['location'] = location.id

        return super(PosProduct, self.with_context(**ctx))._load_pos_data(data)


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
        fields = self._load_pos_data_fields(
            data['pos.config']['data'][0]['id']
        )
        locations = self.search_read(domain, fields, load=False)
        return {
            'data': locations,
            'fields': fields,
        }


class PickingType(models.Model):
    _inherit = 'stock.picking.type'

    @api.model
    def _load_pos_data_fields(self, config_id):
        return super()._load_pos_data_fields(config_id) + [
            'default_location_src_id'
        ]

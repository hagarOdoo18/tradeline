# -*- coding: utf-8 -*-
# Copyright (C) Wisenetic Technologies.

from odoo import api, fields, models

import logging

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    is_display_stock = fields.Boolean(
        related="pos_config_id.is_display_stock",
        string="Display Stock",
        readonly=False)
    stock_type = fields.Selection(
        related="pos_config_id.stock_type", string="Stock Type", help="Select to show on-hand and forecasted stock on the POS screen", readonly=False)
    is_restrict_out_of_stock_products = fields.Boolean(
        related="pos_config_id.is_restrict_out_of_stock_products",
        string="Restrict Product Out of Stock",
        readonly=False)
    is_low_stock_screen_visible = fields.Boolean(
        related='pos_config_id.is_low_stock_screen_visible', readonly=False
    )
    low_stock_threshold = fields.Integer(
        related='pos_config_id.low_stock_threshold', readonly=False)

    low_stock_color = fields.Integer(
        related='pos_config_id.low_stock_color', readonly=False)

    in_stock_color = fields.Integer(
        related='pos_config_id.in_stock_color', readonly=False)

    stock_quantities_refresh_rate = fields.Selection(
        related="pos_config_id.stock_quantities_refresh_rate", required=True, string="Refresh Time", help="Select the interval to refresh stock quantities on the product screen", readonly=False)

    stock_warehouse = fields.Selection(
        related="pos_config_id.stock_warehouse", string="Warehouse Location",
        help="Select the warehouse to manage inventory for this point of sale", readonly=False)

    picking_type_location_id = fields.Many2one(
        related='pos_picking_type_id.default_location_src_id', readonly=False, string="Default Source Location")

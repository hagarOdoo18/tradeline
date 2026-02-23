# -*- coding: utf-8 -*-
# Copyright (C) Wisenetic Technologies.

from email.policy import default
from odoo import fields, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    is_display_stock = fields.Boolean(
        default=True, string="Display Stock in POS", help="Enable if you want to show stock of products")
    stock_type = fields.Selection(
        [('on_hand', 'On Hand'), ('forecasted', 'Forecasted')], default='on_hand',  help="Select to show on-hand and forecasted stock on the POS screen", required=True, string="Stock Type")
    is_restrict_out_of_stock_products = fields.Boolean(
        help="Restrict ordering of out-of-stock products based on displayed quantity",
        string="Restrict Product Out of Stock in POS",
    )
    is_low_stock_screen_visible = fields.Boolean(
        default=True, help="Enable to display products with low stock levels on the POS screen",
        string="Low Stock Products Screen",
    )
    low_stock_threshold = fields.Integer(
        default=5,
        help="Select to show on-hand and forecasted stock on the POS screen",
        required=True, readonly=False)

    low_stock_color = fields.Integer(
        default=1, string='Low Stock Color', help='Color displayed for products with low stock levels.', required=True, readonly=False)

    in_stock_color = fields.Integer(
        default=10, string='In Stock Color', help='Color displayed for products with sufficient stock levels.', required=True, readonly=False)

    update_stock_quantities = fields.Selection(
        related="company_id.point_of_sale_update_stock_quantities", readonly=False)

    stock_quantities_refresh_rate = fields.Selection(
        [('1', '1 Min.'), ('3', '3 Min.'), ('5', '5 Min.'), ('10', '10 Min.')], default='3',  help="Select the interval to refresh stock quantities on the product screen", required=True, string="Refresh Time")

    stock_warehouse = fields.Selection(
        [('all', 'All Warehouses'), ('current', 'Current Session Warehouse')],
        default='all',  help="Select the warehouse to manage inventory for this point of sale", string="Warehouse Location")

    picking_type_location_id = fields.Many2one(related='picking_type_id.default_location_src_id', readonly=False, string="Default Source Location")


# -*- coding: utf-8 -*-
from odoo import fields, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    enable_product_bar_lot_serial_search = fields.Boolean(
        string="Search Products by Lot/Serial in Product Bar",
        default=False,
        help="Allow cashiers to find products from the main POS product bar by typing lot or serial numbers.",
    )


class PosSession(models.Model):
    _inherit = "pos.session"

    def _loader_params_pos_config(self):
        params = super()._loader_params_pos_config()
        fields_to_load = params["search_params"].setdefault("fields", [])
        if "enable_product_bar_lot_serial_search" not in fields_to_load:
            fields_to_load.append("enable_product_bar_lot_serial_search")
        return params

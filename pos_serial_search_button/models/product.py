from odoo import models


class PosSession(models.Model):
    _inherit = "pos.session"

    def _loader_params_stock_lot(self):
        return {
            "search_params": {
                "domain": [],
                "fields": ["id", "name", "product_id"],
            }
        }

    def _get_pos_ui_stock_lot(self, params):
        return self.env["stock.lot"].search_read(
            domain=params["search_params"]["domain"],
            fields=params["search_params"]["fields"],
        )

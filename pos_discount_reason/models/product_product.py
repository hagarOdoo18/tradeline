from odoo import api, models


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields_list = list(super()._load_pos_data_fields(config_id))
        if "family_id" in self._fields and "family_id" not in fields_list:
            fields_list.append("family_id")
        return fields_list

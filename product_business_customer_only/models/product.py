# -*- coding: utf-8 -*-

from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    business_customer_only = fields.Boolean(
        string="Business Customers Only",
        help="If enabled, this product can only be sold to company/business customers.",
    )


class ProductProduct(models.Model):
    _inherit = "product.product"

    business_customer_only = fields.Boolean(
        related="product_tmpl_id.business_customer_only",
        string="Business Customers Only",
        readonly=True,
    )

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields_list = list(super()._load_pos_data_fields(config_id))
        if "business_customer_only" not in fields_list:
            fields_list.append("business_customer_only")
        return fields_list


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields_list = list(super()._load_pos_data_fields(config_id))
        for field_name in ("company_type", "is_company"):
            if field_name in self._fields and field_name not in fields_list:
                fields_list.append(field_name)
        return fields_list

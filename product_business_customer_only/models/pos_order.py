# -*- coding: utf-8 -*-

from odoo import api, models, _
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = "pos.order"

    @staticmethod
    def _extract_m2o_id(value):
        if isinstance(value, dict):
            return value.get("id")
        if isinstance(value, (list, tuple)):
            return value[0] if value else False
        return value

    @staticmethod
    def _extract_line_vals(line_command):
        if isinstance(line_command, (list, tuple)) and len(line_command) == 3:
            return line_command[2] or {}
        if isinstance(line_command, dict):
            return line_command
        return {}

    @api.model
    def _is_pos_business_customer(self, partner_id):
        partner = self.env["res.partner"].browse(partner_id).exists()
        if not partner:
            return False
        partner = partner.commercial_partner_id
        if "company_type" in partner._fields:
            return partner.company_type == "company"
        return bool(partner.is_company)

    @api.model
    def _validate_business_customer_only_pos_order(self, order_payload):
        order_vals = (
            order_payload.get("data")
            if isinstance(order_payload, dict) and order_payload.get("data")
            else order_payload
        )
        if not isinstance(order_vals, dict):
            return

        partner_id = self._extract_m2o_id(order_vals.get("partner_id"))
        if self._is_pos_business_customer(partner_id):
            return

        product_ids = []
        for line_cmd in order_vals.get("lines", []):
            line_vals = self._extract_line_vals(line_cmd)
            product_id = self._extract_m2o_id(line_vals.get("product_id"))
            qty = float(line_vals.get("qty") or 0.0)
            if product_id and qty > 0:
                product_ids.append(product_id)

        if not product_ids:
            return

        products = self.env["product.product"].browse(product_ids).exists().filtered(
            "business_customer_only"
        )
        if products:
            product_names = "\n".join("- %s" % name for name in products.mapped("display_name"))
            raise UserError(_(
                "The following products can only be sold to business customers:\n%s"
            ) % product_names)

    def _process_order(self, order, *args):
        self._validate_business_customer_only_pos_order(order)
        return super()._process_order(order, *args)

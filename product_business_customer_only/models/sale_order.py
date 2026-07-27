# -*- coding: utf-8 -*-

from odoo import api, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _is_business_customer(self):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        if not partner:
            return False
        if "company_type" in partner._fields:
            return partner.company_type == "company"
        return bool(partner.is_company)

    def _get_business_only_lines_for_non_business_customer(self):
        self.ensure_one()
        if self._is_business_customer():
            return self.env["sale.order.line"]
        return self.order_line.filtered(
            lambda line: line.product_id
            and line.product_id.product_tmpl_id.business_customer_only
            and not line.display_type
        )

    def _check_business_customer_only_products(self):
        for order in self:
            restricted_lines = order._get_business_only_lines_for_non_business_customer()
            if restricted_lines:
                product_names = "\n".join(
                    "- %s" % name
                    for name in restricted_lines.mapped("product_id.display_name")
                )
                raise UserError(_(
                    "The following products can only be sold to business customers:\n%s"
                ) % product_names)

    def write(self, vals):
        res = super().write(vals)
        if {"partner_id", "order_line"} & set(vals):
            self._check_business_customer_only_products()
        return res

    def action_confirm(self):
        self._check_business_customer_only_products()
        return super().action_confirm()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    @api.onchange("product_id")
    def _onchange_business_customer_only_product_id(self):
        if (
            self.product_id.business_customer_only
            and self.order_id
            and not self.order_id._is_business_customer()
        ):
            return {
                "warning": {
                    "title": _("Business Customer Required"),
                    "message": _(
                        "Product '%s' can only be sold to business customers."
                    ) % self.product_id.display_name,
                }
            }
        return None

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.order_id._check_business_customer_only_products()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if {"product_id", "order_id"} & set(vals):
            self.order_id._check_business_customer_only_products()
        return res

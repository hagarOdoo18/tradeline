# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    apple_business = fields.Boolean(
        string="Apple Business",
        help="Restrict this order to ABM products for a company with an active Apple Business subscription.",
    )
    apple_business_id = fields.Many2one(
        "apple.business",
        string="Apple Business Subscription",
        domain="[('state', '=', 'active'), ('partner_id', '=', apple_business_company_id), ('branch_id', '=', branch_id)]",
    )
    apple_business_company_id = fields.Many2one(
        "res.partner",
        compute="_compute_apple_business_company_id",
        string="Apple Business Company",
    )

    @api.depends("partner_id")
    def _compute_apple_business_company_id(self):
        for order in self:
            order.apple_business_company_id = order.partner_id.commercial_partner_id

    def _get_active_apple_business_subscription(self):
        self.ensure_one()
        company = self.partner_id.commercial_partner_id
        return self.env["apple.business"].search(
            [
                ("partner_id", "=", company.id),
                ("branch_id", "=", self.branch_id.id),
                ("state", "=", "active"),
            ],
            limit=1,
        )

    def _validate_apple_business_order(self):
        for order in self.filtered("apple_business"):
            company = order.partner_id.commercial_partner_id
            if not company or company.company_type != "company":
                raise UserError(_("Apple Business orders require a company customer."))
            subscription = order._get_active_apple_business_subscription()
            if not subscription:
                raise UserError(_(
                    "The selected customer does not have a confirmed Apple Business "
                    "subscription for this branch."
                ))
            if order.apple_business_id and (
                order.apple_business_id.partner_id != company
                or order.apple_business_id.branch_id != order.branch_id
                or order.apple_business_id.state != "active"
            ):
                raise UserError(_("The selected Apple Business subscription does not belong to this customer or is inactive."))
            non_abm_lines = order.order_line.filtered(
                lambda line: not line.display_type
                and line.product_id
                and (not line.product_id.vendor_id or line.product_id.vendor_id.name.strip().lower() != "abm")
            )
            if non_abm_lines:
                product_names = "\n".join("- %s" % name for name in non_abm_lines.mapped("product_id.display_name"))
                raise UserError(_("Apple Business orders can only contain products supplied by ABM:\n%s") % product_names)

    @api.onchange("partner_id", "branch_id", "apple_business")
    def _onchange_apple_business_partner(self):
        for order in self:
            if not order.apple_business:
                order.apple_business_id = False
                continue
            subscription = order._get_active_apple_business_subscription() if order.partner_id else False
            order.apple_business_id = subscription
            if order.partner_id and not subscription:
                return {
                    "warning": {
                        "title": _("Apple Business Subscription Required"),
                        "message": _(
                            "The selected customer needs a confirmed Apple Business "
                            "subscription for this branch."
                        ),
                    }
                }

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders._validate_apple_business_order()
        return orders

    def write(self, vals):
        result = super().write(vals)
        if {"apple_business", "apple_business_id", "partner_id", "order_line"} & set(vals):
            self._validate_apple_business_order()
        return result

    def action_confirm(self):
        self._validate_apple_business_order()
        return super().action_confirm()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    @api.onchange("product_id")
    def _onchange_apple_business_product_id(self):
        if self.order_id.apple_business and self.product_id and (
            not self.product_id.vendor_id or self.product_id.vendor_id.name.strip().lower() != "abm"
        ):
            return {
                "warning": {
                    "title": _("ABM Product Required"),
                    "message": _("Apple Business orders can only contain products supplied by ABM."),
                }
            }

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.mapped("order_id")._validate_apple_business_order()
        return lines

    def write(self, vals):
        result = super().write(vals)
        if {"product_id", "order_id"} & set(vals):
            self.mapped("order_id")._validate_apple_business_order()
        return result

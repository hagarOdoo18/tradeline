# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError

APPLE_BUSINESS_CATEGORY_NAMES = {"mac", "ipad", "iphone"}


def _category_or_parent_matches(category):
    seen = set()
    current = category
    while current and current.id not in seen:
        if current.name.strip().lower() in APPLE_BUSINESS_CATEGORY_NAMES:
            return True
        seen.add(current.id)
        current = current.parent_id
    return False


def _is_apple_business_product(product):
    return bool(
        product.vendor_id
        and product.vendor_id.name.strip().lower() == "abm"
        and _category_or_parent_matches(product.categ_id)
    )


class SaleOrder(models.Model):
    _inherit = "sale.order"

    apple_business = fields.Boolean(
        string="Apple Business",
        help=(
            "Select only when the company customer has opted into Apple Business "
            "and has a confirmed subscription for this branch."
        ),
    )
    apple_business_id = fields.Many2one(
        "apple.business",
        string="Apple Business Subscription",
        domain="[('state', '=', 'active'), ('partner_id', '=', apple_business_company_id), ('branch_id', '=', branch_id)]",
    )
    apple_business_customer_eligible = fields.Boolean(
        compute="_compute_apple_business_customer_eligible",
        string="Apple Business Customer Eligible",
    )
    apple_business_company_id = fields.Many2one(
        "res.partner",
        compute="_compute_apple_business_company_id",
        string="Apple Business Company",
    )
    apple_business_category_ids = fields.Many2many(
        "product.category",
        compute="_compute_apple_business_category_ids",
        string="Apple Business Product Categories",
    )

    @api.depends("partner_id", "partner_id.is_company")
    def _compute_apple_business_customer_eligible(self):
        for order in self:
            order.apple_business_customer_eligible = bool(
                order.partner_id and order.partner_id.is_company
            )

    @api.depends("partner_id")
    def _compute_apple_business_company_id(self):
        for order in self:
            order.apple_business_company_id = (
                order.partner_id.commercial_partner_id
                if order.partner_id and order.partner_id.is_company
                else False
            )

    def _compute_apple_business_category_ids(self):
        categories = self.env["product.category"].search([])
        allowed_categories = categories.filtered(_category_or_parent_matches)
        for order in self:
            order.apple_business_category_ids = allowed_categories

    def _get_active_apple_business_subscription(self):
        self.ensure_one()
        if not self.apple_business_customer_eligible:
            return self.env["apple.business"]
        company = self.partner_id.commercial_partner_id
        return self.env["apple.business"].search(
            [
                ("partner_id", "=", company.id),
                ("branch_id", "=", self.branch_id.id),
                ("state", "=", "active"),
            ],
            limit=1,
        )

    @api.model
    def get_apple_business_subscription_status(self, partner_id, branch_id):
        partner = self.env["res.partner"].browse(partner_id).exists()
        branch = self.env["res.branch"].browse(branch_id).exists()
        if not partner or not branch or not partner.is_company:
            return {"eligible": False, "subscription_id": False}

        subscription = self.env["apple.business"].search(
            [
                ("partner_id", "=", partner.commercial_partner_id.id),
                ("branch_id", "=", branch.id),
                ("state", "=", "active"),
            ],
            limit=1,
        )
        suggested_invoice = self.env["account.move"].search(
            [
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("commercial_partner_id", "=", partner.commercial_partner_id.id),
                ("branch_id", "=", branch.id),
            ],
            order="invoice_date desc, date desc, id desc",
            limit=1,
        )
        return {
            "eligible": True,
            "subscription_id": subscription.id or False,
            "suggested_invoice_id": suggested_invoice.id or False,
            "suggested_invoice_name": suggested_invoice.name or False,
            "partner_name": partner.name,
            "branch_name": branch.name,
        }

    def _validate_apple_business_order(self):
        for order in self.filtered("apple_business"):
            if not order.apple_business_customer_eligible:
                raise UserError(_("Apple Business orders require a company customer."))
            company = order.partner_id.commercial_partner_id
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
            invalid_apple_lines = order.order_line.filtered(
                lambda line: not line.display_type
                and line.product_id
                and not _is_apple_business_product(line.product_id)
            )
            if invalid_apple_lines:
                product_names = "\n".join(
                    "- %s" % name
                    for name in invalid_apple_lines.mapped("product_id.display_name")
                )
                raise UserError(
                    _(
                        "Apple Business orders can only contain ABM products "
                        "from the Mac, iPad, or iPhone categories:\n%s"
                    )
                    % product_names
                )

    @api.onchange("partner_id", "branch_id", "apple_business")
    def _onchange_apple_business_partner(self):
        for order in self:
            if order.apple_business and not order.apple_business_customer_eligible:
                order.apple_business = False
                order.apple_business_id = False
                return {
                    "warning": {
                        "title": _("Company Customer Required"),
                        "message": _(
                            "Apple Business can only be selected for a company customer. "
                            "The option has been cleared."
                        ),
                    }
                }
            if not order.apple_business:
                order.apple_business_id = False
                continue
            subscription = order._get_active_apple_business_subscription() if order.partner_id else False
            order.apple_business_id = subscription
            if order.partner_id and not subscription:
                order.apple_business = False
                return {
                    "warning": {
                        "title": _("Apple Business Subscription Required"),
                        "message": _(
                            "The selected customer needs a confirmed Apple Business "
                            "subscription for this branch. The option has been cleared."
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
        if {"apple_business", "apple_business_id", "partner_id", "branch_id", "order_line"} & set(vals):
            self._validate_apple_business_order()
        return result

    def action_confirm(self):
        self._validate_apple_business_order()
        return super().action_confirm()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    @api.onchange("product_id")
    def _onchange_apple_business_product_id(self):
        if (
            self.order_id.apple_business
            and self.product_id
            and not _is_apple_business_product(self.product_id)
        ):
            return {
                "warning": {
                    "title": _("Eligible Apple Device Required"),
                    "message": _(
                        "Select an ABM product from the Mac, iPad, or iPhone categories."
                    ),
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

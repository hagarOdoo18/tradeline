# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


class AppleBusiness(models.Model):
    _name = "apple.business"
    _description = "Apple Business Subscription"
    _rec_name = "partner_id"
    _order = "state, partner_id"

    state = fields.Selection(
        [
            ("draft", "Created"),
            ("active", "Confirmed"),
            ("expired", "Expired"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        readonly=True,
        index=True,
    )

    partner_id = fields.Many2one(
        "res.partner",
        string="Company Name",
        required=True,
        domain="[('is_company', '=', True)]",
        ondelete="cascade",
    )
    organization_id = fields.Char(required=True, index=True)
    street = fields.Char(string="Address", related="partner_id.street", readonly=False)
    phone = fields.Char(related="partner_id.phone", readonly=False)
    email = fields.Char(related="partner_id.email", readonly=False)
    country_id = fields.Many2one(
        "res.country", string="Country", related="partner_id.country_id", readonly=False
    )
    invoice_id = fields.Many2one(
        "account.move",
        string="Invoice Number",
        domain="[('move_type', '=', 'out_invoice'), ('state', '=', 'posted'), ('commercial_partner_id', '=', partner_id), ('branch_id', '=', branch_id)]",
        help=(
            "Posted customer invoice used to establish this Apple Business "
            "subscription. You may replace the suggested invoice with another "
            "posted invoice for the same company and branch."
        ),
    )
    branch_id = fields.Many2one("res.branch", string="Branch Name", required=True)
    device_line_ids = fields.One2many(
        "apple.business.device.line", "apple_business_id", string="Products and Serial Numbers", copy=False
    )
    _sql_constraints = [
        (
            "organization_id_branch_unique",
            "unique(organization_id, branch_id)",
            "Organization ID must be unique within each branch.",
        ),
    ]

    def action_confirm(self):
        self._check_manager_access()
        if any(not subscription.invoice_id for subscription in self):
            raise ValidationError(
                _("A posted customer invoice is required before confirming the subscription.")
            )
        self.write({"state": "active"})

    def action_expire(self):
        self._check_manager_access()
        self.write({"state": "expired"})

    def action_cancel(self):
        self._check_manager_access()
        self.write({"state": "cancelled"})

    def action_set_created(self):
        self._check_manager_access()
        self.write({"state": "draft"})

    def _check_manager_access(self):
        if not self.env.su and not self.env.user.has_group("apple_business.group_apple_business_manager"):
            raise AccessError(_("Only Apple Business managers can change a subscription state."))

    @api.constrains("partner_id")
    def _check_company_partner(self):
        for subscription in self:
            if subscription.partner_id and not subscription.partner_id.is_company:
                raise ValidationError(_("Apple Business subscriptions are only available for company customers."))

    @api.constrains("invoice_id", "partner_id", "branch_id")
    def _check_invoice_customer_and_branch(self):
        for subscription in self:
            if not subscription.invoice_id:
                raise ValidationError(
                    _("A posted customer invoice is required for an Apple Business subscription.")
                )
            if (
                subscription.invoice_id.partner_id.commercial_partner_id
                != subscription.partner_id.commercial_partner_id
            ):
                raise ValidationError(_("The Apple Business invoice must belong to the selected company."))
            if subscription.invoice_id.branch_id != subscription.branch_id:
                raise ValidationError(_("The Apple Business invoice must belong to the selected branch."))

    @api.onchange("branch_id")
    def _onchange_branch_id(self):
        for subscription in self:
            if subscription.invoice_id and subscription.invoice_id.branch_id != subscription.branch_id:
                subscription.invoice_id = False

    def _prepare_device_line_commands(self):
        self.ensure_one()
        if not self.invoice_id:
            return [(5, 0, 0)]

        invoice_lines = self.invoice_id.invoice_line_ids.filtered(
            lambda line: line.display_type == "product" and line.product_id
        )
        commands = [(5, 0, 0)]
        serialled_product_ids = set()
        seen_serials = set()
        for lot_values in self.invoice_id._get_invoiced_lot_values():
            lot_id = lot_values.get("lot_id")
            if not lot_id:
                continue
            lot = self.env["stock.lot"].browse(lot_id).exists()
            if not lot:
                continue
            key = (lot.product_id.id, lot.id)
            if key in seen_serials:
                continue
            seen_serials.add(key)
            serialled_product_ids.add(lot.product_id.id)
            commands.append((0, 0, {
                "invoice_line_id": next(
                    (line.id for line in invoice_lines if line.product_id == lot.product_id), False
                ),
                "product_id": lot.product_id.id,
                "lot_id": lot.id,
                "quantity": 1.0 if lot.product_id.tracking == "serial" else 0.0,
            }))

        for invoice_line in invoice_lines:
            if invoice_line.product_id.id not in serialled_product_ids:
                commands.append((0, 0, {
                    "invoice_line_id": invoice_line.id,
                    "product_id": invoice_line.product_id.id,
                    "quantity": invoice_line.quantity,
                }))
        return commands

    def _sync_device_lines_from_invoice(self):
        for subscription in self:
            commands = subscription._prepare_device_line_commands()
            subscription.device_line_ids.sudo().unlink()
            line_values = [
                {
                    **command[2],
                    "apple_business_id": subscription.id,
                }
                for command in commands
                if command[0] == 0
            ]
            if line_values:
                self.env["apple.business.device.line"].sudo().create(line_values)

    @api.onchange("invoice_id")
    def _onchange_invoice_id(self):
        for subscription in self:
            subscription.device_line_ids = subscription._prepare_device_line_commands()

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [
            {key: value for key, value in vals.items() if key != "device_line_ids"}
            for vals in vals_list
        ]
        if not self.env.su and any(
            vals.get("state", "draft") != "draft" for vals in vals_list
        ):
            self._check_manager_access()
        subscriptions = super().create(vals_list)
        subscriptions._sync_device_lines_from_invoice()
        return subscriptions

    def write(self, vals):
        vals = {key: value for key, value in vals.items() if key != "device_line_ids"}
        if "state" in vals:
            self._check_manager_access()
        result = super().write(vals)
        if "invoice_id" in vals:
            self._sync_device_lines_from_invoice()
        return result


class AppleBusinessDeviceLine(models.Model):
    _name = "apple.business.device.line"
    _description = "Apple Business Device"
    _order = "product_id, lot_id"

    apple_business_id = fields.Many2one("apple.business", required=True, ondelete="cascade")
    invoice_line_id = fields.Many2one("account.move.line", string="Invoice Line", readonly=True)
    product_id = fields.Many2one("product.product", string="Product", required=True, readonly=True)
    lot_id = fields.Many2one("stock.lot", string="Serial Number", readonly=True)
    quantity = fields.Float(readonly=True)

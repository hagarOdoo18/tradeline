# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    preorder_id = fields.Many2one(
        "sale.preorder",
        string="Pre-order Delivery",
        copy=False,
        readonly=True,
        index=True,
        ondelete="set null",
    )
    preorder_source_order_id = fields.Many2one(
        "sale.order",
        string="Original Pre-order Quotation",
        copy=False,
        readonly=True,
        index=True,
        ondelete="restrict",
    )
    preorder_record_ids = fields.One2many(
        "sale.preorder", "source_order_id", string="Pre-order Records"
    )
    preorder_record_count = fields.Integer(compute="_compute_preorder_record_count")
    is_preorder_quotation = fields.Boolean(compute="_compute_is_preorder_quotation")

    @api.depends("preorder_record_ids", "preorder_id")
    def _compute_preorder_record_count(self):
        for order in self:
            order.preorder_record_count = len(order.preorder_record_ids) + (1 if order.preorder_id else 0)

    @api.depends("inv_type", "order_line.product_id", "order_line.name")
    def _compute_is_preorder_quotation(self):
        for order in self:
            order.is_preorder_quotation = bool(
                order.inv_type == "quotation" and order._has_downpayment_product_lines()
            )

    def action_create_preorder_record(self):
        self.ensure_one()
        if self.preorder_record_ids:
            return {
                "type": "ir.actions.act_window",
                "name": _("Customer Pre-order"),
                "res_model": "sale.preorder",
                "view_mode": "form",
                "res_id": self.preorder_record_ids[:1].id,
            }
        if not self.is_preorder_quotation:
            raise UserError(_("Only quotations containing a Down Payment product can become pre-orders."))

        today = fields.Date.context_today(self)
        campaign_domain = [
            ("company_id", "=", self.company_id.id),
            ("state", "in", ("draft", "open", "allocation")),
            ("date_start", "<=", today),
            ("date_end", ">=", today),
        ]
        campaigns = self.env["sale.preorder.campaign"].search(campaign_domain)
        if self.branch_id:
            campaigns = campaigns.filtered(
                lambda campaign: not campaign.branch_ids or self.branch_id in campaign.branch_ids
            )
        context = {"default_source_order_id": self.id}
        if len(campaigns) == 1:
            context["default_campaign_id"] = campaigns.id
        return {
            "type": "ir.actions.act_window",
            "name": _("New Customer Pre-order"),
            "res_model": "sale.preorder",
            "view_mode": "form",
            "target": "current",
            "context": context,
        }

    def action_open_preorder_record(self):
        self.ensure_one()
        preorder = self.preorder_id or self.preorder_record_ids[:1]
        if not preorder:
            raise UserError(_("No pre-order record is linked to this sales order."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Customer Pre-order"),
            "res_model": "sale.preorder",
            "view_mode": "form",
            "res_id": preorder.id,
        }

    def _prepare_invoice(self):
        values = super()._prepare_invoice()
        if self.preorder_id:
            values["preorder_id"] = self.preorder_id.id
        return values

    def _build_downpayment_source_domain(self, *args, **kwargs):
        domain = super()._build_downpayment_source_domain(*args, **kwargs)
        domain += [
            "|",
            ("preorder_record_ids", "=", False),
            ("preorder_record_ids.state", "=", "cancelled"),
        ]
        return domain

    def _get_valid_downpayment_lines(self, source_quotation, *args, **kwargs):
        active_preorders = source_quotation.preorder_record_ids.filtered(
            lambda preorder: preorder.state != "cancelled"
        )
        if active_preorders:
            raise UserError(
                _(
                    "This quotation is controlled by Pre-order Management and cannot also be loaded "
                    "through the legacy Down Payment workflow."
                )
            )
        return super()._get_valid_downpayment_lines(source_quotation, *args, **kwargs)


class PosOrder(models.Model):
    _inherit = "pos.order"

    @api.model
    def _build_downpayment_source_domain_pos(self, *args, **kwargs):
        domain = super()._build_downpayment_source_domain_pos(*args, **kwargs)
        domain += [
            "|",
            ("preorder_record_ids", "=", False),
            ("preorder_record_ids.state", "=", "cancelled"),
        ]
        return domain


class AccountMove(models.Model):
    _inherit = "account.move"

    preorder_id = fields.Many2one(
        "sale.preorder",
        string="Customer Pre-order",
        copy=False,
        readonly=True,
        index=True,
        ondelete="set null",
    )

# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = "account.payment"

    preorder_payment_id = fields.Many2one(
        "sale.preorder",
        string="Customer Pre-order",
        index=True,
        copy=False,
        ondelete="restrict",
    )

    @api.depends(
        "reconciled_invoice_ids",
        "reconciled_invoice_ids.branch_id",
        "move_id",
        "move_id.branch_id",
        "sale_order_id",
        "preorder_payment_id",
        "preorder_payment_id.branch_id",
    )
    def compute_branches(self):
        super().compute_branches()
        for payment in self.filtered("preorder_payment_id"):
            payment.branch_id = payment.preorder_payment_id.branch_id
            if payment.move_id:
                payment.move_id.branch_id = payment.preorder_payment_id.branch_id

    def _validate_preorder_payment_identity(self):
        for payment in self.filtered("preorder_payment_id"):
            preorder = payment.preorder_payment_id
            if payment.payment_type != "inbound" or payment.partner_type != "customer":
                raise UserError(_("A Customer Pre-order can only use an inbound customer payment."))
            if payment.company_id != preorder.company_id:
                raise UserError(_("Payment and Customer Pre-order must use the same company."))
            if payment.currency_id != preorder.currency_id:
                raise UserError(_("Payment and Customer Pre-order must use the same currency."))
            if payment.partner_id.commercial_partner_id != preorder.customer_id.commercial_partner_id:
                raise UserError(_("Payment and Customer Pre-order must use the same customer."))
            if payment.branch_id and payment.branch_id != preorder.branch_id:
                raise UserError(_("Payment and Customer Pre-order must use the same branch."))

    def action_post(self):
        self._validate_preorder_payment_identity()
        result = super().action_post()
        self.mapped("preorder_payment_id")._sync_payment_readiness()
        return result

    def action_draft(self):
        preorders = self.mapped("preorder_payment_id")
        result = super().action_draft()
        preorders._sync_payment_readiness()
        return result

    def action_cancel(self):
        preorders = self.mapped("preorder_payment_id")
        result = super().action_cancel()
        preorders._sync_payment_readiness()
        return result

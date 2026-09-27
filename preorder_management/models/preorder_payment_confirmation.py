# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class SalePreorderPaymentConfirmation(models.Model):
    _name = "sale.preorder.payment.confirmation"
    _description = "Pre-order Payment Confirmation"
    _order = "source_date, id"

    preorder_id = fields.Many2one(
        "sale.preorder", required=True, ondelete="cascade", index=True
    )
    source_payment_id = fields.Many2one(
        "account.payment", readonly=True, ondelete="restrict", index=True,
        help="Posted accounting payment for a migrated legacy pre-order. New delivery-mode pre-orders leave this empty until POS delivery.",
    )
    reversal_payment_id = fields.Many2one(
        "account.payment", readonly=True, ondelete="restrict", index=True
    )
    amount = fields.Monetary(required=True, readonly=True)
    currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    journal_id = fields.Many2one("account.journal", readonly=True)
    payment_channel = fields.Char(readonly=True)
    source_date = fields.Date(readonly=True)
    source_reference = fields.Char(readonly=True)
    state = fields.Selection(
        [("confirmed", "Confirmed"), ("consumed", "Consumed")],
        default="confirmed",
        required=True,
        readonly=True,
        copy=False,
        index=True,
    )
    _sql_constraints = [
        (
            "source_payment_unique",
            "unique(source_payment_id)",
            "Each original payment can be migrated only once.",
        ),
    ]

    def _check_payment_method_correction_access(self):
        self.ensure_one()
        preorder = self.preorder_id
        if not (
            self.env.user == preorder.create_uid
            or self.env.user.has_group("preorder_management.group_preorder_manager")
        ):
            raise AccessError(_("Only the pre-order creator or a Pre-order Manager can correct its payment method."))
        if (
            preorder.payment_recording_mode != "delivery"
            or self.source_payment_id
            or self.state != "confirmed"
            or preorder.state not in ("confirmed", "pending", "allocated")
            or preorder.final_sale_order_id
            or preorder.fulfillment_pos_order_id
        ):
            raise UserError(_("This payment method can no longer be corrected before delivery."))

    def action_correct_payment_method(self):
        self._check_payment_method_correction_access()
        return {
            "type": "ir.actions.act_window",
            "name": _("Correct Pre-order Payment Method"),
            "res_model": "sale.preorder.payment.method.correction",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_confirmation_id": self.id,
                "default_journal_id": self.journal_id.id,
                "default_reference": self.source_reference,
            },
        }

    @api.constrains("amount", "currency_id", "preorder_id")
    def _check_identity(self):
        for confirmation in self:
            if confirmation.amount <= 0:
                raise ValidationError(_("A payment confirmation must have a positive amount."))
            if confirmation.currency_id != confirmation.preorder_id.currency_id:
                raise ValidationError(_("The confirmation currency must match the pre-order currency."))

# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SalePreorderPaymentConfirmation(models.Model):
    _name = "sale.preorder.payment.confirmation"
    _description = "Pre-order Payment Confirmation"
    _order = "source_date, id"

    preorder_id = fields.Many2one(
        "sale.preorder", required=True, ondelete="cascade", index=True
    )
    source_payment_id = fields.Many2one(
        "account.payment", required=True, readonly=True, ondelete="restrict", index=True
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
    delivery_payment_ids = fields.One2many(
        "account.payment", "preorder_delivery_id", string="Delivery Payments", readonly=True
    )

    _sql_constraints = [
        (
            "source_payment_unique",
            "unique(source_payment_id)",
            "Each original payment can be migrated only once.",
        ),
    ]

    @api.constrains("amount", "currency_id", "preorder_id")
    def _check_identity(self):
        for confirmation in self:
            if confirmation.amount <= 0:
                raise ValidationError(_("A payment confirmation must have a positive amount."))
            if confirmation.currency_id != confirmation.preorder_id.currency_id:
                raise ValidationError(_("The confirmation currency must match the pre-order currency."))


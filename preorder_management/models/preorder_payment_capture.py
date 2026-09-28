# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class SalePreorderPaymentCapture(models.TransientModel):
    """Record a customer's pre-order payment without posting accounting entries.

    The money is recorded as a confirmed deposit on the pre-order and is posted
    to accounting only when the branch completes delivery in POS.  This keeps
    the original payment channel and reference for audit while avoiding a
    posted payment that later has to be re-dated or reversed.
    """

    _name = "sale.preorder.payment.capture"
    _description = "Confirm Pre-order Payment"

    preorder_id = fields.Many2one(
        "sale.preorder", required=True, readonly=True, ondelete="cascade"
    )
    company_id = fields.Many2one(related="preorder_id.company_id", readonly=True)
    currency_id = fields.Many2one(related="preorder_id.currency_id", readonly=True)
    amount = fields.Monetary(required=True)
    journal_id = fields.Many2one(
        "account.journal",
        string="Payment Channel",
        required=True,
        domain="[('company_id', '=', company_id), ('type', 'in', ('bank', 'cash'))]",
    )
    payment_date = fields.Date(
        string="Confirmation Date", required=True, default=fields.Date.context_today
    )
    reference = fields.Char(string="External Reference")

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        preorder_id = self.env.context.get("active_id")
        preorder = self.env["sale.preorder"].browse(preorder_id).exists()
        if preorder:
            values.update(
                {
                    "preorder_id": preorder.id,
                    "amount": preorder.payment_due_amount,
                    "currency_id": preorder.currency_id.id,
                    "company_id": preorder.company_id.id,
                }
            )
        return values

    @api.constrains("amount")
    def _check_amount(self):
        for wizard in self:
            if wizard.amount <= 0:
                raise ValidationError(_("The confirmed payment must be positive."))

    def action_confirm(self):
        self.ensure_one()
        preorder = self.preorder_id
        if not preorder:
            raise UserError(_("The pre-order no longer exists."))
        if preorder.state != "confirmed":
            raise UserError(_("Only confirmed pre-orders can receive a payment confirmation."))
        if preorder.payment_recording_mode != "delivery":
            raise UserError(
                _("This pre-order uses the legacy accounting payment workflow. Migrate it before using a non-posted confirmation.")
            )
        preorder.invalidate_recordset(["payment_due_amount", "payment_status"])
        due = preorder.payment_due_amount
        if self.amount > due:
            raise UserError(
                _("The confirmed amount cannot exceed the remaining amount of %(amount).2f %(currency)s.")
                % {"amount": due, "currency": preorder.currency_id.name}
            )
        journal = self.journal_id
        if not journal or journal.company_id != preorder.company_id:
            raise UserError(_("Select a bank or cash journal belonging to the pre-order company."))
        self.env["sale.preorder.payment.confirmation"].sudo().create(
            {
                "preorder_id": preorder.id,
                "amount": self.amount,
                "currency_id": preorder.currency_id.id,
                "journal_id": journal.id,
                "payment_channel": journal.display_name,
                "source_date": self.payment_date,
                "source_reference": self.reference or preorder.name,
                "state": "confirmed",
            }
        )
        preorder._sync_payment_readiness()
        return {"type": "ir.actions.client", "tag": "reload"}

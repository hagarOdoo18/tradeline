# -*- coding: utf-8 -*-

from odoo import fields, models, _
from odoo.exceptions import UserError


class SalePreorderPaymentMethodCorrection(models.TransientModel):
    _name = "sale.preorder.payment.method.correction"
    _description = "Correct Pre-order Payment Method"

    confirmation_id = fields.Many2one(
        "sale.preorder.payment.confirmation", required=True, readonly=True
    )
    company_id = fields.Many2one(related="confirmation_id.preorder_id.company_id")
    journal_id = fields.Many2one(
        "account.journal",
        string="Payment Method",
        required=True,
        domain="[('company_id', '=', company_id), ('type', 'in', ('bank', 'cash'))]",
    )
    reference = fields.Char(string="External Reference")

    def action_apply(self):
        self.ensure_one()
        confirmation = self.confirmation_id.exists()
        if not confirmation:
            raise UserError(_("The payment confirmation no longer exists."))
        self.env.cr.execute(
            "SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE",
            [confirmation.preorder_id.id],
        )
        confirmation.preorder_id.invalidate_recordset(
            ["state", "final_sale_order_id", "fulfillment_pos_order_id"]
        )
        confirmation.invalidate_recordset(["state", "source_payment_id"])
        confirmation._check_payment_method_correction_access()
        journal = self.journal_id
        if journal.company_id != confirmation.preorder_id.company_id or journal.type not in ("bank", "cash"):
            raise UserError(_("Select a bank or cash journal belonging to the pre-order company."))
        old_method = confirmation.payment_channel or confirmation.journal_id.display_name
        old_reference = confirmation.source_reference or ""
        new_reference = self.reference or ""
        if journal == confirmation.journal_id and new_reference == old_reference:
            return {"type": "ir.actions.act_window_close"}
        confirmation.sudo().write({
            "journal_id": journal.id,
            "payment_channel": journal.display_name,
            "source_reference": new_reference,
        })
        confirmation.preorder_id.message_post(body=_(
            "Pre-order payment method corrected from %(old)s to %(new)s. "
            "External reference: %(old_ref)s → %(new_ref)s. No accounting payment was changed."
        ) % {
            "old": old_method,
            "new": journal.display_name,
            "old_ref": old_reference or _("None"),
            "new_ref": new_reference or _("None"),
        })
        return {"type": "ir.actions.client", "tag": "reload"}

# -*- coding: utf-8 -*-

from odoo import _, models


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_post(self):
        result = super().action_post()
        if len(self) != 1:
            return result

        invoice = self
        if invoice.move_type != "out_invoice" or invoice.state != "posted":
            return result

        apple_business_orders = invoice.invoice_line_ids.sale_line_ids.order_id.filtered(
            "apple_business"
        )
        if not apple_business_orders:
            return result

        partner = invoice.commercial_partner_id
        branch = invoice.branch_id
        active_subscription = self.env["apple.business"].search(
            [
                ("partner_id", "=", partner.id),
                ("branch_id", "=", branch.id),
                ("state", "=", "active"),
            ],
            limit=1,
        )
        if active_subscription:
            return result

        return {
            "type": "ir.actions.client",
            "tag": "apple_business_subscription_prompt",
            "params": {
                "partner_id": partner.id,
                "partner_name": partner.display_name,
                "branch_id": branch.id,
                "branch_name": branch.display_name,
                "invoice_id": invoice.id,
                "invoice_name": invoice.display_name,
            },
        }

# -*- coding: utf-8 -*-

from odoo import _, models


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_post(self):
        result = super().action_post()
        if (
            self.env.context.get("apple_business_subscription_flow")
            and len(self) == 1
            and self.move_type == "out_invoice"
            and self.state == "posted"
        ):
            return {
                "type": "ir.actions.act_window",
                "name": _("New Apple Business Subscription"),
                "res_model": "apple.business",
                "views": [[False, "form"]],
                "target": "new",
                "context": {
                    "default_partner_id": self.commercial_partner_id.id,
                    "default_branch_id": self.branch_id.id,
                    "default_invoice_id": self.id,
                },
            }
        return result

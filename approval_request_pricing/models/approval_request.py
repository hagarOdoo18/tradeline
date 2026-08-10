from odoo import fields, models


class ApprovalRequest(models.Model):
    _inherit = "approval.request"

    payment_term_id = fields.Many2one(
        comodel_name="account.payment.term",
        string="Payment Terms",
        check_company=True,
    )
    payment_method_type_id = fields.Many2one(
        comodel_name="account.payment.method",
        string="Method Type",
        domain=[("payment_type", "=", "outbound")],
    )

from odoo import fields, models


class ApprovalRequest(models.Model):
    _inherit = "approval.request"

    payment_term_option_id = fields.Many2one(
        comodel_name="approval.payment.term.option",
        string="Payment Terms",
        ondelete="restrict",
    )
    method_type_option_id = fields.Many2one(
        comodel_name="approval.method.type.option",
        string="Method Type",
        ondelete="restrict",
    )

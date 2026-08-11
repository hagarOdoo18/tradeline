from odoo import fields, models


class ApprovalRequest(models.Model):
    _inherit = "approval.request"

    payment_terms = fields.Char(
        string="Payment Terms",
    )
    method_type = fields.Char(
        string="Method Type",
    )

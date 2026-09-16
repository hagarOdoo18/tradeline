from odoo import fields, models


class ApprovalPaymentTermOption(models.Model):
    _name = "approval.payment.term.option"
    _description = "Approval Payment Term Option"
    _order = "sequence, name"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_unique", "unique(name)", "The payment terms option must be unique."),
    ]


class ApprovalMethodTypeOption(models.Model):
    _name = "approval.method.type.option"
    _description = "Approval Method Type Option"
    _order = "sequence, name"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_unique", "unique(name)", "The method type option must be unique."),
    ]
